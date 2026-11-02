"""Execution authority follows the live runner and fencing, never elapsed time."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    Limits,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)

DAY = 24 * 60 * 60 * 1000


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             actor or Actor(role=Role.OPERATOR))


@pytest.fixture
async def case(tmp_path):
    clock = [1_000_000]
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path, clock=lambda: clock[0])
    try:
        await command(hub, "task.publish", {"contract": {
            "title": "Ownership fixture", "objective": "Keep the admitted execution's authority."}})
        run = await command(hub, "run.start")
        attempt = await hub.next_assignment(run["id"])
        actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                      fencing_token=attempt["fencing_token"])
        yield SimpleNamespace(hub=hub, run=run, attempt=attempt, actor=actor, clock=clock)
    finally:
        await hub.close()


async def test_worker_authority_survives_arbitrary_elapsed_time(case):
    case.clock[0] += 30 * DAY
    assert (await case.hub.authorize_worker(case.actor))["id"] == case.attempt["id"]


async def test_stale_fencing_token_is_still_rejected(case):
    stale = case.actor.model_copy(update={"fencing_token": case.actor.fencing_token + 1})
    with pytest.raises(HubError) as error:
        await case.hub.authorize_worker(stale)
    assert error.value.code == "STALE_BINDING"


async def test_readiness_still_gates_actual_worker_operations(case, monkeypatch):
    def unavailable(*args):
        raise HubError("LEAN_INITIALIZATION_REQUIRED", "Synthetic missing readiness")
    monkeypatch.setattr(case.hub, "_execution_environment", unavailable)
    with pytest.raises(HubError) as error:
        await case.hub.authorize_worker(case.actor)
    assert error.value.code == "LEAN_INITIALIZATION_REQUIRED"


async def test_slow_preparation_still_dispatches_its_turn(case, monkeypatch):
    harness = ScriptedHarness([ScriptedTurn([])])
    runner = WorkerRunner(case.hub, lambda: harness)
    entered, release = asyncio.Event(), asyncio.Event()
    original = harness.open

    async def slow_open(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(harness, "open", slow_open)
    running = asyncio.create_task(runner.run_once(case.run["id"], assignment=case.attempt))
    try:
        async with asyncio.timeout(30):
            await entered.wait()
            case.clock[0] += 7 * DAY  # Far beyond any former lease.
            release.set()
            result = await running
        assert result["turn_status"] == "completed" and len(harness.prompts) == 1
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def test_retired_lease_and_turn_limits_are_rejected():
    for retired in ({"lease_seconds": 90}, {"max_turns": 5}):
        with pytest.raises(ValueError):
            Limits.model_validate({**retired, "packet_bytes": 4096})
