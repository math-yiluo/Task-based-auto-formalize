"""Model-free preparation failures and cancellation never dispatch a turn."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             Actor(role=Role.OPERATOR))


@pytest.fixture
async def case(tmp_path):
    clock = [1_000_000]
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path, clock=lambda: clock[0])
    await command(hub, "task.publish", {"contract": {
        "title": "Preparation lease fixture", "objective": "Retain the exact execution fence."}})
    run = await command(hub, "run.start")
    attempt = await hub.next_assignment(run["id"])
    harness = ScriptedHarness([ScriptedTurn([])])
    runner = WorkerRunner(hub, lambda: harness)
    try:
        yield SimpleNamespace(hub=hub, run=run, attempt=attempt, harness=harness,
                              runner=runner, clock=clock)
    finally:
        await hub.close()


def block_stage(monkeypatch, case, stage):
    owner, name = {
        "recovery": (case.hub, "pending_native_validations"),
        "harness": (case.runner, "_new_harness"),
        "catalog": (case.runner, "_ensure_catalog"),
        "open": (case.harness, "open"),
        "context": (case.hub, "context"),
        "native_packet": (case.runner, "_native_packet"),
        "prompt": (case.runner, "_present"),
    }[stage]
    entered, release, collected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = getattr(owner, name)

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
            return await original(*args, **kwargs)
        finally:
            collected.set()

    monkeypatch.setattr(owner, name, blocked)
    return entered, release, collected


async def test_preflight_waits_for_original_but_leaves_retries_to_driver(case, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    recovered = []

    async def pending(*args, **kwargs):
        return [{"validation_generation": generation} for generation in (0, 1, 7)]

    async def recover(item):
        generation = item["validation_generation"]
        if generation:
            raise OSError("An older candidate's rebuild failed")
        recovered.append(generation)
        entered.set()
        await release.wait()

    monkeypatch.setattr(case.hub, "pending_native_validations", pending)
    monkeypatch.setattr(case.runner, "_recover_submission", recover)
    running = asyncio.create_task(case.runner.run_once(case.run["id"], assignment=case.attempt))
    try:
        async with asyncio.timeout(30):
            await entered.wait()
            assert not case.harness.prompts
            release.set()
            await running
        assert recovered == [0] and len(case.harness.prompts) == 1
        assert (await case.hub.run(case.run["id"]))["state"] == "running"
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("stage", ["catalog", "open", "native_packet", "prompt"])
async def test_preparation_failure_never_dispatches(case, monkeypatch, stage):
    entered, release, collected = block_stage(monkeypatch, case, stage)
    owner, name = {"catalog": (case.runner, "_ensure_catalog"), "open": (case.harness, "open"),
                   "native_packet": (case.runner, "_native_packet"), "prompt": (case.runner, "_present")}[stage]
    blocked = getattr(owner, name)

    async def failing(*args, **kwargs):
        if stage == "prompt" and not args:
            return await blocked(*args, **kwargs)
        entered.set()
        collected.set()
        raise OSError("Fixture preparation failed")

    monkeypatch.setattr(owner, name, failing)
    async with asyncio.timeout(30):
        with pytest.raises(OSError, match="preparation failed"):
            await case.runner.run_once(case.run["id"], assignment=case.attempt)
    assert collected.is_set()
    assert not case.harness.prompts and case.harness.process_quiet
    run = await case.hub.run(case.run["id"])
    assert run["state"] == "running" and not run["usage_uncertain"]
    intents = await case.hub.db.call(lambda c: [dict(r) for r in c.execute("SELECT * FROM turn_intents")])
    assert all(intent["state"] == "not_sent" and intent["remote_turn_id"] is None for intent in intents)
    assert (await case.hub.worker(case.attempt["worker_id"]))["active_attempt"] is None
    assert not case.runner._executing


async def test_cancellation_collects_committed_turn_reservation(case, monkeypatch):
    committed, release = asyncio.Event(), asyncio.Event()
    original = case.runner._command

    async def delayed_reservation(name, *args, **kwargs):
        result = await original(name, *args, **kwargs)
        if name == "turn.reserve":
            committed.set()
            await release.wait()
        return result

    monkeypatch.setattr(case.runner, "_command", delayed_reservation)
    running = asyncio.create_task(case.runner.run_once(case.run["id"], assignment=case.attempt))
    try:
        async with asyncio.timeout(30):
            await committed.wait()
            running.cancel()
            await asyncio.sleep(0.05)
            assert not running.done()  # The committed reservation result is still collected.
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
        intent = await case.hub.db.call(lambda c: dict(c.execute("SELECT * FROM turn_intents").fetchone()))
        assert intent["state"] == "not_sent" and intent["remote_turn_id"] is None
        assert not case.harness.prompts and case.harness.process_quiet
        assert not (await case.hub.run(case.run["id"]))["usage_uncertain"]
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_preflight_pause_is_held_without_model_or_uncertain_usage(case, monkeypatch):
    entered, release, collected = block_stage(monkeypatch, case, "open")
    running = asyncio.create_task(case.runner.run_once(case.run["id"], assignment=case.attempt))
    try:
        async with asyncio.timeout(30):
            await entered.wait()
            await command(case.hub, "run.pause", {"run_id": case.run["id"]})
            release.set()
            await asyncio.gather(running, return_exceptions=True)
        assert collected.is_set() and not case.harness.prompts
        run = await case.hub.run(case.run["id"])
        assert run["state"] == "paused" and not run["usage_uncertain"] and run["turns_used"] == 0
        assert (await case.hub.worker(case.attempt["worker_id"]))["active_attempt"] is None
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_repeated_cancellation_collects_native_open_cleanup(case, monkeypatch):
    entered, cleaning, release, collected = (asyncio.Event() for _ in range(4))
    original = case.harness.open

    async def blocked_open(**kwargs):
        await original(**kwargs)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            await case.harness.close()
            collected.set()

    monkeypatch.setattr(case.harness, "open", blocked_open)
    running = asyncio.create_task(case.runner.run_once(case.run["id"], assignment=case.attempt))
    try:
        async with asyncio.timeout(30):
            await entered.wait()
            running.cancel()
            await cleaning.wait()
            await asyncio.sleep(0.05)
            running.cancel()
            await asyncio.sleep(0.05)
            assert not running.done() and not collected.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert collected.is_set() and case.harness.process_quiet
            assert not case.harness.prompts and not case.runner._executing
            assert not any(task.get_name() == "worker-preflight"
                           for task in asyncio.all_tasks() if task is not asyncio.current_task())
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_repeated_cancellation_collects_threaded_prompt_preparation(case, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = case.runner.continuation_prompts.prepare

    def blocked_prompt(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(30), "Fixture did not release prompt preparation"
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(case.runner.continuation_prompts, "prepare", blocked_prompt)
    running = asyncio.create_task(case.runner.run_once(case.run["id"], assignment=case.attempt))
    try:
        async with asyncio.timeout(30):
            assert await asyncio.to_thread(entered.wait, 20)
            running.cancel()
            await asyncio.sleep(0.05)
            running.cancel()
            await asyncio.sleep(0.05)
            assert not running.done() and not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert finished.is_set()
            assert not case.harness.prompts and case.harness.process_quiet
            assert not case.runner._executing
            assert not any(task.get_name() == "worker-preflight"
                           for task in asyncio.all_tasks() if task is not asyncio.current_task())
        intent = await case.hub.db.call(lambda c: dict(c.execute("SELECT * FROM turn_intents").fetchone()))
        assert intent["state"] == "not_sent"
        assert not (await case.hub.run(case.run["id"]))["usage_uncertain"]
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
