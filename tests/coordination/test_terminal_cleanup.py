"""A known, drained native outcome survives slower local receipt collection."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    CodexConfig,
    Command,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    TurnOutcome,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.codex import CodexHarness, SteerOutcome
from tests.coordination.test_rpc_backpressure import feed, memory_peer


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             Actor(role=Role.OPERATOR))


@pytest.fixture
async def case(tmp_path):
    initialize(tmp_path, ProjectConfig(codex=CodexConfig(stop_grace_seconds=0.05)))
    hub = Hub(tmp_path)
    await command(hub, "task.publish", {"contract": {
        "title": "Terminal cleanup fixture", "objective": "Keep exact terminal and usage evidence."}})
    run = await command(hub, "run.start")
    entered, release, terminal, collected = (asyncio.Event() for _ in range(4))

    class Harness(ScriptedHarness):
        active_turn_id = None
        status = "completed"
        fail_after_terminal = False
        release_on_terminal = False
        samples = (17,)
        usage_wait = 0.0

        def set_native_handler(self, callback):
            self.native = callback

        async def steer(self, text, *, expected_turn_id, **kwargs):
            assert self.active_turn_id is None
            return SteerOutcome("deferred", expected_turn_id, "Turn already completed")

        async def run_turn(self, prompt, *, on_started, on_usage, **kwargs):
            self.prompts.append(prompt)
            self.active_turn_id = "exact-native-turn"
            await on_started(self.active_turn_id)
            started = time.monotonic()
            for total in self.samples:
                await on_usage({"totalTokens": total})
            self.usage_wait = time.monotonic() - started
            await entered.wait()
            await self.native("turn/completed", {
                "threadId": self.session.thread_id,
                "turn": {"id": self.active_turn_id, "status": self.status}})
            self.active_turn_id = None
            await self.close()
            terminal.set()
            if self.release_on_terminal:
                release.set()
            if self.fail_after_terminal:
                raise RuntimeError("Native terminal alone cannot prove final usage drainage")
            return TurnOutcome("exact-native-turn", self.status, True, True,
                               visible_output="Exact final reply")

    harness = Harness()
    runner = WorkerRunner(hub, lambda: harness)
    try:
        yield SimpleNamespace(hub=hub, run=run, harness=harness, runner=runner,
                              entered=entered, release=release, terminal=terminal, collected=collected)
    finally:
        release.set()
        await hub.close()


def delayed_steering(case, monkeypatch, after=None):
    async def collect(harness, attempt, session, started):
        await started.wait()
        case.entered.set()
        try:
            await case.release.wait()
            if after is not None:
                await after(attempt, session)
        finally:
            case.collected.set()

    monkeypatch.setattr(case.runner, "_steer_messages", collect)


async def state(case):
    return await case.hub.db.call(lambda c: {
        "intent": dict(c.execute("SELECT * FROM turn_intents").fetchone()),
        "samples": [dict(row) for row in c.execute("SELECT * FROM usage_samples")],
        "terminals": [dict(row) for row in c.execute("SELECT * FROM events WHERE kind='turn.finished'")],
    })


async def test_post_terminal_local_work_can_exceed_shutdown_grace(case, monkeypatch):
    delayed_steering(case, monkeypatch)
    running = asyncio.create_task(case.runner.run_once(case.run["id"]))
    try:
        async with asyncio.timeout(20):
            await case.terminal.wait()
            await asyncio.sleep(1.3)  # Longer than the old stop_grace_seconds + 1 deadline.
            before = await state(case)
            assert not running.done() and not before["terminals"]
            assert before["intent"]["state"] == "bound"
            assert not (await case.hub.run(case.run["id"]))["usage_uncertain"]
            case.release.set()
            result = await running
            assert result["turn_status"] == "completed"
            assert case.collected.is_set() and case.harness.process_quiet
            after = await state(case)
            assert after["intent"]["state"] == "completed"
            assert after["intent"]["remote_turn_id"] == "exact-native-turn"
            assert len(after["terminals"]) == 1
            assert [sample["total"] for sample in after["samples"]] == [17]
            assert len(case.harness.prompts) == 1
    finally:
        case.release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
async def test_local_receipt_error_preserves_returned_terminal_and_private_diagnostic(case, monkeypatch, status):
    case.harness.status = status

    async def failure(*args):
        raise RuntimeError("Fixture local receipt failure")

    delayed_steering(case, monkeypatch, failure)
    running = asyncio.create_task(case.runner.run_once(case.run["id"]))
    try:
        async with asyncio.timeout(20):
            await case.terminal.wait()
            # Let the returned native outcome reach post-turn collection.
            await asyncio.sleep(0.05)
            case.release.set()
            result = await running
            assert result["turn_status"] == status
        records = await state(case)
        assert records["intent"]["state"] == status
        assert len(records["terminals"]) == 1
        run = await case.hub.run(case.run["id"])
        assert run["state"] == "running" and not run["usage_uncertain"] and run["tokens_used"] == 17
        events = [json.loads(line) for line in case.runner.activity.path.read_text().splitlines()]
        error = next(event for event in events if event["event"] == "runtime_error")
        diagnostic = case.hub.blobs.get_json(error["diagnostic_sha256"])
        assert diagnostic["phase"] == "post_turn_steering"
        assert 1 <= len(diagnostic["frames"]) <= 8
        assert all(set(frame) == {"file", "line", "function"} for frame in diagnostic["frames"])
    finally:
        case.release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_cancellation_after_returned_terminal_does_not_make_usage_unknown(case, monkeypatch):
    delayed_steering(case, monkeypatch)
    running = asyncio.create_task(case.runner.run_once(case.run["id"]))
    try:
        async with asyncio.timeout(20):
            await case.terminal.wait()
            await asyncio.sleep(0.05)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        assert case.collected.is_set() and case.harness.process_quiet
        records = await state(case)
        assert records["intent"]["state"] == "completed"
        assert not (await case.hub.run(case.run["id"]))["usage_uncertain"]
    finally:
        case.release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_simultaneous_background_error_and_native_return_keep_known_outcome(case, monkeypatch):
    async def failure(*args):
        raise RuntimeError("Fixture simultaneous receipt failure")

    case.harness.release_on_terminal = True
    delayed_steering(case, monkeypatch, failure)
    async with asyncio.timeout(20):
        assert (await case.runner.run_once(case.run["id"]))["turn_status"] == "completed"
    assert (await state(case))["intent"]["state"] == "completed"
    assert not (await case.hub.run(case.run["id"]))["usage_uncertain"]


@pytest.mark.parametrize("hold", ["usage", "delivery"])
async def test_known_terminal_does_not_clear_independent_uncertainty(case, monkeypatch, hold):
    async def held(attempt, session):
        if hold == "usage":
            result = await case.runner._command("usage.record", {
                "worker_id": attempt["worker_id"], "sample": {"totalTokens": 16}})
            assert result["uncertain"]
        else:
            await command(case.hub, "message.send", {
                "worker_id": attempt["worker_id"], "message_id": "held-input", "text": "Exact text"})
            delivery = await case.hub.human_unread(case.runner._worker_actor(attempt))
            await case.hub.mark_delivery_uncertain(
                Actor(role=Role.RUNNER), "human", delivery["delivery_id"], session.thread_id,
                "exact-native-turn", "ambiguous-steer")
            await command(case.hub, "run.pause", {"run_id": case.run["id"]})
        raise RuntimeError("Fixture independent uncertainty")

    delayed_steering(case, monkeypatch, held)
    running = asyncio.create_task(case.runner.run_once(case.run["id"]))
    try:
        async with asyncio.timeout(20):
            await case.terminal.wait()
            await asyncio.sleep(0.05)
            case.release.set()
            assert (await running)["turn_status"] == "completed"
        assert (await state(case))["intent"]["state"] == "completed"
        run = await case.hub.run(case.run["id"])
        assert run["state"] == ("running" if hold == "usage" else "paused")
        assert bool(run["usage_uncertain"]) is (hold == "usage")
        if hold == "delivery":
            delivery = await case.hub.db.call(lambda c: dict(c.execute(
                "SELECT * FROM message_delivery_holds WHERE input_id='ambiguous-steer'").fetchone()))
            assert delivery["state"] == "uncertain"
    finally:
        case.release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_terminal_event_without_successful_harness_return_remains_uncertain(case, monkeypatch):
    case.harness.fail_after_terminal = True
    delayed_steering(case, monkeypatch)
    async with asyncio.timeout(20):
        with pytest.raises(RuntimeError, match="final usage drainage"):
            await case.runner.run_once(case.run["id"])
    assert (await state(case))["intent"]["state"] == "closed_unknown"
    assert (await case.hub.run(case.run["id"]))["usage_uncertain"]
    # Finished as unknown, then closed when its execution ended: held once, not twice.
    holds = await case.hub.db.call(lambda c: [json.loads(row[0])["reason"] for row in c.execute(
        "SELECT payload_json FROM events WHERE kind='worker.accounting_held'")])
    assert holds == ["Native turn outcome is unknown"]


async def test_usage_is_recorded_in_order_before_the_turn_finishes(case, monkeypatch):
    # Recording a sample no longer holds the native notifications (the terminal among them)
    # while the coordination database is slow; the turn still finishes with all of them.
    case.harness.samples = (5, 11, 17)
    case.harness.release_on_terminal = True
    delayed_steering(case, monkeypatch)
    original, commands = case.runner._command, []

    async def slow(name, payload, **kwargs):
        if name == "usage.record":
            await asyncio.sleep(0.2)
            commands.append(payload["sample"]["totalTokens"])
        elif name == "turn.finish":
            commands.append(name)
        return await original(name, payload, **kwargs)

    monkeypatch.setattr(case.runner, "_command", slow)
    async with asyncio.timeout(20):
        assert (await case.runner.run_once(case.run["id"]))["turn_status"] == "completed"
    assert case.harness.usage_wait < 0.2
    assert commands == [5, 11, 17, "turn.finish"]
    records = await state(case)
    assert records["intent"]["state"] == "completed"
    assert [sample["total"] for sample in records["samples"]] == [17]
    run = await case.hub.run(case.run["id"])
    assert run["tokens_used"] == 17 and not run["usage_uncertain"]
    end = await case.hub.db.call(lambda c: c.execute("SELECT end_total FROM turn_usage_totals").fetchone()[0])
    assert end == 17


async def test_close_finishes_received_messages_after_the_process_exits():
    # A notification handler that outlasts the stop grace (a usage record waiting for the
    # database) no longer loses the messages queued behind it, the turn's terminal among them.
    entered, release, handled = asyncio.Event(), asyncio.Event(), []

    async def callback(method, params):
        handled.append(params["sequence"])
        if params["sequence"] == 0:
            entered.set()
            await release.wait()

    harness = CodexHarness(CodexConfig(stop_grace_seconds=0.02), command=["unused"])
    harness.received_drain_seconds = 10.0
    host, rpc = memory_peer(notification_handler=callback)
    harness.rpc = rpc
    try:
        feed(host, [{"method": "event", "params": {"sequence": sequence}} for sequence in range(2)])
        host.stdout.feed_eof()
        host.stderr.feed_eof()
        await entered.wait()
        closing = asyncio.create_task(harness.close())
        await asyncio.sleep(0.2)  # Ten stop graces.
        assert not closing.done() and handled == [0]
        release.set()
        async with asyncio.timeout(5):
            await closing
        assert handled == [0, 1]
    finally:
        release.set()
        await rpc.close()


async def test_rpc_callback_timeout_names_pending_work_without_cancelling_it():
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(*args):
        entered.set()
        await release.wait()

    host, rpc = memory_peer(notification_handler=callback)
    try:
        feed(host, [{"method": "event", "params": {}}])
        await entered.wait()
        with pytest.raises(TimeoutError, match=r"draining callbacks \(requests=0, notifications=1\)"):
            await rpc.drain_callbacks(timeout=0.01)
        release.set()
        await rpc.drain_callbacks()
        assert rpc.failure is None
    finally:
        await rpc.close()


async def test_rpc_pipe_timeout_names_unfinished_stream():
    host, rpc = memory_peer()
    try:
        host.stdout.feed_eof()
        with pytest.raises(TimeoutError, match=r"stdout_done=True, stderr_done=False"):
            await rpc.finish_receiving(timeout=0.01)
        host.stderr.feed_eof()
        await rpc.finish_receiving()
    finally:
        await rpc.close()
