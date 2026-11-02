"""Local runtime failures remain isolated; unknown inference and usage stay held."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    TurnOutcome,
    WorkerRunner,
)
from formal_geyser.coordination.storage import canonical
from tests.coordination.lifecycle_helpers import assignment_decision, idle_worker


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             Actor(role=Role.OPERATOR))


@pytest.fixture
async def runtime(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    await command(hub, "task.publish", {"contract": {
        "title": "Synthetic task", "objective": "Inspect exact runtime failure boundaries."}})
    run = await command(hub, "run.start")
    yield hub, run
    await hub.close()


async def helper_intent(hub, run):
    """The prepared assignment decision for the published task, offering a new idle worker.

    At most one assignment decision is live at a time.
    """
    idle = await idle_worker(hub, run, "Earlier task")
    intent = await assignment_decision(hub, run["id"])
    assert idle["id"] in [item["worker_id"] for item in intent["candidates"]["workers"]], (
        "Expected a new assignment decision that offers the new idle worker")
    return intent


@pytest.mark.parametrize("failure", ["factory", "open"])
async def test_helper_preflight_failure_has_no_inference_uncertainty(runtime, failure):
    hub, run = runtime
    intent = await helper_intent(hub, run)
    calls = []

    class BrokenOpen(ScriptedHarness):
        async def open(self, **kwargs):
            raise OSError("Synthetic local preflight failure before any turn")

        async def run_turn(self, *args, **kwargs):
            calls.append("inference")
            raise AssertionError("No turn may be dispatched")

    def factory():
        if failure == "factory":
            raise OSError("Synthetic local factory failure")
        return BrokenOpen()

    with pytest.raises(OSError):
        await WorkerRunner(hub, factory).run_helper(intent)
    current = await hub.run(run["id"])
    assert not calls and current["turns_used"] == 0
    assert current["state"] == "running" and not current["usage_uncertain"]
    held = await hub.db.call(lambda c: hub._helper(c, intent["id"]))
    assert held["state"] == "uncertain"
    # The held decision is not replayed, and no second decision or new worker replaces it.
    assert await hub.next_assignment(run["id"]) is None
    assert await hub.due_assignment_intents(run["id"]) == []


@pytest.mark.parametrize("status", ["failed", "interrupted", "cancelled", "completed", "unknown"])
async def test_known_helper_terminals_are_not_unknown_inference(runtime, status):
    hub, run = runtime
    intent = await helper_intent(hub, run)
    harness = ScriptedHarness([ScriptedTurn([], status=status, visible_output="not a decision")])
    runner = WorkerRunner(hub, lambda: harness)
    if status == "completed":
        # A completed answer outside the schema counts as no suitable worker: a new one.
        result = await runner.run_helper(intent)
        assert result["outcome"] == "new_worker" and result["reason"] == "none_suitable"
    else:
        with pytest.raises(HubError):
            await runner.run_helper(intent)
    current = await hub.run(run["id"])
    expected_uncertain = status == "unknown"
    assert current["state"] == "running"
    assert bool(current["usage_uncertain"]) is expected_uncertain
    assert harness.process_quiet


@pytest.mark.parametrize("failure", ["malformed_sample", "ledger_write"])
async def test_known_helper_completion_keeps_decision_despite_accounting_warning(runtime, monkeypatch, failure):
    hub, run = runtime
    intent = await helper_intent(hub, run)
    [offered] = intent["candidates"]["workers"]
    callback_errors = []

    class AccountingFailure(ScriptedHarness):
        async def run_turn(self, prompt, *, on_started, on_usage, **kwargs):
            await on_started("known-helper-turn")
            sample = ({"totalTokens": 4} if failure == "malformed_sample" else
                      {"totalTokens": 4, "lastTokenUsage": {"totalTokens": 4}})
            try:
                await on_usage(sample)
            except Exception as exc:
                callback_errors.append(exc)
            await self.close()
            return TurnOutcome("known-helper-turn", "completed", True, True,
                               error="Retained usage callback error",
                               final_output=canonical({"worker": offered["name"]}))

    harness = AccountingFailure()
    runner = WorkerRunner(hub, lambda: harness)
    original = runner._command

    async def fail_usage(name, *args, **kwargs):
        if name == "helper.usage":
            raise OSError("Synthetic ledger failure after native usage observation")
        return await original(name, *args, **kwargs)

    if failure == "ledger_write":
        monkeypatch.setattr(runner, "_command", fail_usage)
    result = await runner.run_helper(intent)
    assert result["directive"] == "assigned" and result["outcome"] == "idle_worker"
    assert result["assignment"]["worker_id"] == offered["worker_id"]
    assert not callback_errors and harness.process_quiet
    current = await hub.run(run["id"])
    assert current["state"] == "running" and current["usage_uncertain"]
    held = await hub.db.call(lambda c: hub._helper(c, intent["id"]))
    assert held["state"] == "completed" and held["raw_result_json"] is not None


async def test_worker_prompt_preparation_failure_does_not_claim_unknown_inference(runtime, monkeypatch):
    hub, run = runtime
    calls = []

    class NoInference(ScriptedHarness):
        async def run_turn(self, *args, **kwargs):
            calls.append("inference")
            raise AssertionError("No native input may be dispatched")

    runner = WorkerRunner(hub, NoInference)

    def fail_prompt(*args, **kwargs):
        raise OSError("Synthetic local input-file write failed before turn dispatch")

    monkeypatch.setattr(runner.transcripts, "prepare_input", fail_prompt)
    with pytest.raises(OSError):
        await runner.run_once(run["id"])
    current = await hub.run(run["id"])
    assert not calls
    assert current["state"] == "running" and not current["usage_uncertain"]
    intents = await hub.db.call(lambda c: [dict(row) for row in c.execute("SELECT * FROM turn_intents")])
    assert len(intents) == 1 and intents[0]["state"] == "not_sent" and intents[0]["remote_turn_id"] is None
    assert current["turns_used"] == 1 and current["tokens_used"] == 0
    assert all(worker["active_attempt"] is None for worker in await hub.list_workers())


@pytest.mark.parametrize("dispatched", [False, True])
async def test_worker_inbox_holds_distinguish_unsent_preparation_from_dispatched_unknown(runtime, monkeypatch, dispatched):
    hub, run = runtime
    attempt = await hub.next_assignment(run["id"])
    await command(hub, "message.send", {"worker_id": attempt["worker_id"], "message_id": "input",
                  "text": "Read this entire direction before acting."})
    calls = []

    class BrokenTurn(ScriptedHarness):
        async def run_turn(self, *args, **kwargs):
            calls.append("native_dispatch")
            raise OSError("Synthetic transport failed after inference dispatch")

    harness = BrokenTurn()
    runner = WorkerRunner(hub, lambda: harness)
    original_hold = runner._hold_inbox

    async def fail_after_guard(*args, **kwargs):
        await original_hold(*args, **kwargs)
        raise OSError("Synthetic local failure after writing the input's transport guards")

    if not dispatched:
        monkeypatch.setattr(runner, "_hold_inbox", fail_after_guard)
    with pytest.raises(OSError):
        await runner.run_once(run["id"], assignment=attempt)
    assert harness.process_quiet and bool(calls) is dispatched
    current = await hub.run(run["id"])
    assert current["state"] == "running"
    assert bool(current["usage_uncertain"]) is dispatched
    records = await hub.db.call(lambda c: {
        "turn": dict(c.execute("SELECT * FROM turn_intents").fetchone()),
        "hold": dict(c.execute("SELECT * FROM message_delivery_holds").fetchone()),
        "message": dict(c.execute("SELECT * FROM human_messages").fetchone())})
    assert records["turn"]["state"] == ("closed_unknown" if dispatched else "not_sent")
    assert records["hold"]["state"] == ("uncertain" if dispatched else "not_delivered")
    assert records["message"]["acknowledged_offset"] == 0 and records["message"]["acknowledged_at"] is None
    unread = await hub.human_unread(Actor(role=Role.OPERATOR), worker_id=attempt["worker_id"])
    assert bool(unread["messages"]) is not dispatched
    statuses = [event["status"] for event in runner.transcripts.page(attempt["worker_id"])["events"]
                if event["type"] == "input_status"]
    assert statuses[-1] == ("uncertain" if dispatched else "not_delivered")
    if not dispatched:
        assert "uncertain" not in statuses


async def test_pause_during_input_preparation_stops_dispatch_without_uncertain_usage(runtime, monkeypatch):
    hub, run = runtime
    calls = []

    class NoInference(ScriptedHarness):
        async def run_turn(self, *args, **kwargs):
            calls.append("native_dispatch")
            raise AssertionError("Pause must prevent the prepared input from being sent")

    runner = WorkerRunner(hub, NoInference)
    original = runner._hold_inbox

    async def pause_after_preparation(*args, **kwargs):
        await original(*args, **kwargs)
        await command(hub, "run.pause", {"run_id": run["id"]})

    monkeypatch.setattr(runner, "_hold_inbox", pause_after_preparation)
    with pytest.raises(HubError, match="Run no longer permits"):
        await runner.run_once(run["id"])
    current = await hub.run(run["id"])
    assert not calls and current["state"] == "paused" and not current["usage_uncertain"]
    assert await hub.db.call(lambda c: c.execute("SELECT state FROM turn_intents").fetchone()[0]) == "not_sent"


@pytest.mark.parametrize("invalid", ["operator", "dispatched", "not_quiet", "bound", "uncertain"])
async def test_not_sent_evidence_cannot_clear_authoritative_or_ambiguous_native_execution(runtime, invalid):
    hub, run = runtime
    attempt = await hub.next_assignment(run["id"])
    await hub.db.call(lambda c: c.execute("UPDATE workers SET thread_id='native-thread' WHERE id=?",
                                         (attempt["worker_id"],)), write=True)
    runner = Actor(role=Role.RUNNER)

    async def internal(name, payload, actor=runner):
        return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload), actor)

    intent = await internal("turn.reserve", {"worker_id": attempt["worker_id"]})
    expected_state = "prepared"
    if invalid == "bound":
        await internal("turn.bind", {"intent_id": intent["id"], "remote_turn_id": "actual-native-turn"})
        expected_state = "bound"
    elif invalid == "uncertain":
        await internal("turn.finish", {"intent_id": intent["id"], "status": "unknown"})
        expected_state = "uncertain"
    payload = {"intent_id": intent["id"], "worker_id": attempt["worker_id"], "attempt_id": attempt["id"],
               "thread_id": "native-thread", "input_id": None, "inference_dispatched": invalid == "dispatched",
               "process_quiet": invalid != "not_quiet", "reason": "Local preparation failed before dispatch."}
    actor = Actor(role=Role.OPERATOR) if invalid == "operator" else runner
    with pytest.raises(HubError):
        await internal("turn.not_sent", payload, actor)
    saved = await hub.db.call(lambda c: dict(c.execute("SELECT * FROM turn_intents WHERE id=?", (intent["id"],)).fetchone()))
    assert saved["state"] == expected_state
    if invalid == "uncertain":
        current = await hub.run(run["id"])
        assert current["state"] == "running" and current["usage_uncertain"]


async def test_independent_cancelled_child_does_not_cancel_healthy_driver_work(runtime):
    hub, run = runtime
    finished = False

    class Runner(WorkerRunner):
        admitted = 0

        async def _select(self, _):
            self.admitted += 1
            return {"id": str(self.admitted)} if self.admitted <= 2 else None

        async def run_once(self, run_id, *, assignment=None):
            nonlocal finished
            if assignment["id"] == "1":
                raise asyncio.CancelledError
            await asyncio.sleep(0.05)
            finished = True
            return {"directive": "waiting", "turn_status": "completed"}

    result = await Runner(hub).run_until_idle(run["id"])
    assert finished and result["failure_observed"] and result["state"] == "needs_attention"
    assert result["steps"] == 2


async def test_explicit_driver_cancellation_still_waits_for_all_children_cleanup(runtime):
    hub, run = runtime
    started, cleaned = set(), set()
    ready = asyncio.Event()

    class Runner(WorkerRunner):
        admitted = 0

        async def _select(self, _):
            self.admitted += 1
            return {"id": str(self.admitted)} if self.admitted <= 2 else None

        async def run_once(self, run_id, *, assignment=None):
            identity = assignment["id"]
            started.add(identity)
            if len(started) == 2:
                ready.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0.01)
                cleaned.add(identity)

    driver = asyncio.create_task(Runner(hub).run_until_idle(run["id"]))
    await asyncio.wait_for(ready.wait(), 5)
    driver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(driver, 5)
    assert cleaned == started == {"1", "2"}
