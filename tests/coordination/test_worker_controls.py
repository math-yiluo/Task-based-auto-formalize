"""Pausing or resuming one person against its run, without model execution."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    TurnOutcome,
    WorkerRunner,
)
from tests.coordination.publication_helpers import new_hub

OP = Actor(role=Role.OPERATOR)


async def command(hub, name, payload=None, actor=OP):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


def worker_actor(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


@pytest.fixture
async def case(tmp_path):
    hub = await new_hub(tmp_path, config=ProjectConfig(), clock=lambda: 10_000)
    root = await command(hub, "task.publish", {"contract": {"title": "Task 0", "objective": "Satisfy the original goal."}})
    run = await command(hub, "run.start")
    yield hub, root, run
    await hub.close()


async def finish_first_turn(hub, attempt):
    """Complete a first turn and decline recruitment, leaving the worker ready to continue."""
    await hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    helpers = await hub.due_recruitment_intents(attempt["run_id"])
    assert len(helpers) == 1
    await hub.decide_recruitment(helpers[0]["id"], False, "")


async def two_ready_workers(hub, run):
    first = await hub.next_assignment(run["id"])
    await finish_first_turn(hub, first)
    task = await command(hub, "task.publish", {"contract": {"title": "Second task", "objective": "Produce the stated result."}})
    worker = await command(hub, "worker.register")
    second = await command(hub, "task.claim", {"task_id": task["id"], "revision": 1, "run_id": run["id"],
                                               "worker_id": worker["id"]})
    await finish_first_turn(hub, second)
    return first, second


async def active(hub, attempt):
    return await hub.db.call(lambda c: hub._active(c, worker_actor(attempt)))


async def test_paused_run_admits_only_individually_resumed_workers_own_work(case):
    hub, root, run = case
    first, second = await two_ready_workers(hub, run)
    await command(hub, "run.pause", {"run_id": run["id"]})
    assert await hub.next_assignment(run["id"]) is None
    resumed = await command(hub, "worker.resume", {"worker_id": second["worker_id"], "run_id": run["id"]})
    assert (resumed["control"], resumed["state"], resumed["run_state"]) == ("resumed", "running", "paused")
    control = await hub.run(run["id"])
    assert control["state"] == "paused" and control["resumed_workers"] == [second["worker_id"]]
    assert (await hub.run(run["id"], worker_id=second["worker_id"]))["state"] == "running"
    assert (await hub.run(run["id"], worker_id=first["worker_id"]))["state"] == "paused"
    workers = {item["id"]: item for item in await hub.list_workers()}
    assert workers[second["worker_id"]]["run_control"] == "resumed"
    assert workers[first["worker_id"]]["run_control"] is None
    assignment = await hub.next_assignment(run["id"])
    assert (assignment["worker_id"], assignment["task_id"]) == (second["worker_id"], second["task_id"])
    await active(hub, assignment)  # Its own tool calls are accepted while the run is paused.
    assert await hub.next_assignment(run["id"]) is None  # Others, new tasks, and fresh workers wait.
    # Pausing it again returns it to the run's pause; its turn may no longer act.
    paused = await command(hub, "worker.pause", {"worker_id": second["worker_id"], "run_id": run["id"]})
    assert (paused["control"], paused["state"]) == (None, "paused")
    with pytest.raises(HubError) as stopped:
        await active(hub, assignment)
    assert stopped.value.code == "RUN_PAUSED"
    await command(hub, "worker.resume", {"worker_id": first["worker_id"], "run_id": run["id"]})
    # Resume all ends every individual control.
    await command(hub, "run.resume", {"run_id": run["id"]})
    assert (await hub.run(run["id"]))["resumed_workers"] == []
    assert all(item["run_control"] is None for item in await hub.list_workers())
    again = await hub.next_assignment(run["id"])
    assert again["worker_id"] == first["worker_id"]


async def test_individually_paused_worker_waits_while_the_run_continues(case):
    hub, root, run = case
    first, second = await two_ready_workers(hub, run)
    paused = await command(hub, "worker.pause", {"worker_id": first["worker_id"]})  # The current run.
    assert (paused["run_id"], paused["control"], paused["state"]) == (run["id"], "paused", "paused")
    control = await hub.run(run["id"], worker_id=first["worker_id"])
    assert (control["state"], control["run_state"], control["stop_reason"]) == ("paused", "running", "operator_pause")
    assert (await hub.run(run["id"]))["paused_workers"] == [first["worker_id"]]
    assignment = await hub.next_assignment(run["id"])
    assert assignment["worker_id"] == second["worker_id"]
    assert await hub.next_assignment(run["id"]) is None
    with pytest.raises(HubError) as refused:
        await command(hub, "task.join", {"task_id": root["id"], "revision": 1, "run_id": run["id"],
                                         "worker_id": first["worker_id"]})
    assert refused.value.code == "WORKER_PAUSED"
    # Pause all, then Resume all: the individual pause ends with them.
    await command(hub, "run.pause", {"run_id": run["id"]})
    await command(hub, "run.resume", {"run_id": run["id"]})
    assert (await hub.run(run["id"]))["paused_workers"] == []
    again = await hub.next_assignment(run["id"])
    assert again["worker_id"] == first["worker_id"]


async def test_worker_controls_are_operator_commands_on_a_live_run(case):
    hub, root, run = case
    first = await hub.next_assignment(run["id"])
    with pytest.raises(HubError) as forbidden:
        await command(hub, "worker.pause", {"worker_id": first["worker_id"], "run_id": run["id"]}, worker_actor(first))
    assert forbidden.value.status == 403
    # Resuming a worker that already follows a running run records nothing.
    unchanged = await command(hub, "worker.resume", {"worker_id": first["worker_id"], "run_id": run["id"]})
    assert (unchanged["control"], unchanged["state"]) == (None, "running")
    await command(hub, "run.stop", {"run_id": run["id"]})
    with pytest.raises(HubError) as stopped:
        await command(hub, "worker.resume", {"worker_id": first["worker_id"], "run_id": run["id"]})
    assert stopped.value.code == "INVALID_RUN_TRANSITION"


async def test_pausing_one_worker_parks_its_turn_and_resuming_continues_it(case):
    hub, root, run = case
    started, stopped = asyncio.Event(), asyncio.Event()

    class LongHarness(ScriptedHarness):
        async def interrupt(self):
            stopped.set()

        async def run_turn(self, prompt, *, on_tool, on_usage, on_started):
            turn_id = uuid4().hex
            await on_started(turn_id)
            await on_usage({"totalTokens": 20})
            started.set()
            await stopped.wait()
            await self.close()
            return TurnOutcome(turn_id, "interrupted", True, True)

    running = asyncio.create_task(WorkerRunner(hub, LongHarness).run_until_idle(run["id"]))
    try:
        await asyncio.wait_for(started.wait(), 15)
        worker = next(item for item in await hub.list_workers() if item["active_attempt"])
        await command(hub, "worker.pause", {"worker_id": worker["id"], "run_id": run["id"]})
        result = await asyncio.wait_for(running, 15)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    parked = result["results"][0]
    assert (parked["directive"], parked["stop_reason"]) == ("parked", "operator_pause")
    assert (await hub.run(run["id"]))["state"] == "running"  # Only this person stopped.
    assert [item["state"] for item in await hub.list_interruptions()] == ["parked"]
    assert await hub.next_assignment(run["id"]) is None  # Its parked turn waits for it.
    await command(hub, "worker.resume", {"worker_id": worker["id"], "run_id": run["id"]})
    resumed = await hub.next_assignment(run["id"])
    assert resumed["worker_id"] == worker["id"] and resumed["interruption_id"]


async def test_project_shutdown_overrides_an_individual_resume(case):
    hub, root, run = case
    first = await hub.next_assignment(run["id"])
    await finish_first_turn(hub, first)
    await command(hub, "run.pause", {"run_id": run["id"]})
    await command(hub, "worker.resume", {"worker_id": first["worker_id"], "run_id": run["id"]})
    await hub.db.call(lambda c: c.execute("UPDATE project_lifecycle SET state='shutting_down' WHERE id=1"), write=True)
    assert (await hub.run(run["id"], worker_id=first["worker_id"]))["state"] == "paused"
    assert (await hub.run(run["id"]))["resumed_workers"] == []
    assert await hub.next_assignment(run["id"]) is None
