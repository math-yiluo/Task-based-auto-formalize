"""Pause after turn: turns already under way finish on their own, and nothing starts after them."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, ProjectConfig, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    TurnOutcome,
    WorkerRunner,
)
from tests.coordination.publication_helpers import new_hub
from tests.coordination.test_runtime_failure_isolation import helper_intent
from tests.coordination.test_runtime_failure_isolation import runtime as runtime
from tests.coordination.test_worker_controls import active, command, two_ready_workers, worker_actor

RUNNER = Actor(role=Role.RUNNER)
DECISION = json.dumps({"worker": None})  # An assignment decision: no idle worker suits the task.


@pytest.fixture
async def case(tmp_path):
    hub = await new_hub(tmp_path, config=ProjectConfig(), clock=lambda: 10_000)
    root = await command(hub, "task.publish", {"contract": {"title": "Task 0", "objective": "Satisfy the original goal."}})
    run = await command(hub, "run.start")
    yield hub, root, run
    await hub.close()


async def turn_under_way(hub, attempt):
    """Reserve and bind a model turn for an attempt, as the runner does when it dispatches."""
    intent = await command(hub, "turn.reserve", {"worker_id": attempt["worker_id"]}, RUNNER)
    intent = intent.get("intent", intent)
    await command(hub, "turn.bind", {"intent_id": intent["id"], "remote_turn_id": uuid4().hex}, RUNNER)
    return intent


async def finish(hub, intent):
    await command(hub, "turn.finish", {"intent_id": intent["id"], "status": "completed",
                                       "terminal_observed": True, "process_quiet": True}, RUNNER)


async def dispatch(hub, attempt):
    return await hub.authorize_worker(worker_actor(attempt), dispatch=True)


async def test_one_person_paused_after_turn_acts_until_its_turn_ends(case):
    hub, root, run = case
    first, second = await two_ready_workers(hub, run)
    assignment = await hub.next_assignment(run["id"])
    other = second if assignment["worker_id"] == first["worker_id"] else first
    intent = await turn_under_way(hub, assignment)
    paused = await command(hub, "worker.pause_after_turn", {"worker_id": assignment["worker_id"], "run_id": run["id"]})
    assert (paused["control"], paused["state"], paused["pause_mode"]) == ("paused", "paused", "after_turn")
    control = await hub.run(run["id"], worker_id=assignment["worker_id"])
    assert (control["state"], control["run_state"], control["pause_mode"]) == ("paused", "running", "after_turn")
    assert (await hub.run(run["id"]))["paused_workers"] == [assignment["worker_id"]]
    worker = await hub.worker(assignment["worker_id"])
    assert worker["run_control"] == "paused" and worker["run_control_after_turn"] is True
    listed = {item["id"]: item for item in await hub.list_workers()}
    assert listed[assignment["worker_id"]]["run_control_after_turn"]
    assert not listed[other["worker_id"]]["run_control_after_turn"]
    await active(hub, assignment)  # Its turn under way still acts.
    with pytest.raises(HubError) as refused:
        await dispatch(hub, assignment)  # No input is sent for another turn.
    assert refused.value.code == "RUN_PAUSED"
    # Asking again keeps the pause it has.
    again = await command(hub, "worker.pause_after_turn", {"worker_id": assignment["worker_id"], "run_id": run["id"]})
    assert again["pause_mode"] == "after_turn"
    await finish(hub, intent)
    with pytest.raises(HubError) as ended:
        await active(hub, assignment)  # After its turn it may no longer act.
    assert ended.value.code == "RUN_PAUSED"
    # Everyone else continues.
    assert (await hub.next_assignment(run["id"]))["worker_id"] == other["worker_id"]
    assert await hub.next_assignment(run["id"]) is None
    await command(hub, "worker.resume", {"worker_id": assignment["worker_id"], "run_id": run["id"]})
    assert (await hub.worker(assignment["worker_id"]))["run_control"] is None
    await active(hub, assignment)


async def test_pause_now_overrides_a_pause_after_turn(case):
    hub, root, run = case
    first, _ = await two_ready_workers(hub, run)
    assignment = await hub.next_assignment(run["id"])
    await turn_under_way(hub, assignment)
    await command(hub, "worker.pause_after_turn", {"worker_id": assignment["worker_id"], "run_id": run["id"]})
    now = await command(hub, "worker.pause", {"worker_id": assignment["worker_id"], "run_id": run["id"]})
    assert (now["control"], now["pause_mode"]) == ("paused", "now")
    assert (await hub.worker(assignment["worker_id"]))["run_control_after_turn"] is False
    with pytest.raises(HubError) as stopped:
        await active(hub, assignment)
    assert stopped.value.code == "RUN_PAUSED"


async def test_pause_all_after_turn_admits_nothing_new_and_lets_turns_under_way_finish(case):
    hub, root, run = case
    first, second = await two_ready_workers(hub, run)
    assignment = await hub.next_assignment(run["id"])
    other = second if assignment["worker_id"] == first["worker_id"] else first
    intent = await turn_under_way(hub, assignment)
    paused = await command(hub, "run.pause_after_turn", {"run_id": run["id"]})
    assert (paused["state"], paused["pause_after_turn"]) == ("paused", 1)
    control = await hub.run(run["id"], worker_id=assignment["worker_id"])
    assert (control["state"], control["stop_reason"], control["pause_mode"]) == ("paused", "operator_pause", "after_turn")
    assert (await hub.run(run["id"]))["pause_mode"] == "after_turn"
    await active(hub, assignment)
    with pytest.raises(HubError):
        await dispatch(hub, assignment)
    assert await hub.next_assignment(run["id"]) is None  # No new turns, tasks or people.
    # One person resumed alone runs; one paused now while the run finishes turns is interrupted.
    await command(hub, "worker.resume", {"worker_id": other["worker_id"], "run_id": run["id"]})
    assert (await hub.next_assignment(run["id"]))["worker_id"] == other["worker_id"]
    now = await command(hub, "worker.pause", {"worker_id": assignment["worker_id"], "run_id": run["id"]})
    assert (now["control"], now["pause_mode"]) == ("paused", "now")
    with pytest.raises(HubError):
        await active(hub, assignment)
    # Pause all after turn again ends individual controls; its turn may finish once more.
    await command(hub, "run.pause_after_turn", {"run_id": run["id"]})
    await active(hub, assignment)
    await finish(hub, intent)
    with pytest.raises(HubError):
        await active(hub, assignment)
    # Pause all now ends the after-turn mode, and Resume all ends the pause.
    hard = await command(hub, "run.pause", {"run_id": run["id"]})
    assert hard["pause_after_turn"] == 0
    assert (await hub.run(run["id"], worker_id=assignment["worker_id"]))["pause_mode"] == "now"
    resumed = await command(hub, "run.resume", {"run_id": run["id"]})
    assert (resumed["state"], resumed["pause_after_turn"]) == ("running", 0)
    assert (await hub.run(run["id"]))["pause_mode"] is None
    await active(hub, assignment)


async def test_a_person_resumed_under_pause_all_can_pause_after_its_turn(case):
    hub, root, run = case
    await two_ready_workers(hub, run)
    await command(hub, "run.pause", {"run_id": run["id"]})
    assignment_owner = (await hub.list_workers())[0]["id"]
    await command(hub, "worker.resume", {"worker_id": assignment_owner, "run_id": run["id"]})
    assignment = await hub.next_assignment(run["id"])
    await turn_under_way(hub, assignment)
    paused = await command(hub, "worker.pause_after_turn", {"worker_id": assignment["worker_id"], "run_id": run["id"]})
    assert (paused["control"], paused["pause_mode"]) == ("paused", "after_turn")
    await active(hub, assignment)  # Not interrupted by the run's own pause.


class Gate(ScriptedHarness):
    """One model turn that waits for the test, then checkpoints and completes on its own."""

    def __init__(self, started, release, interrupted):
        super().__init__()
        self.started, self.release, self.interrupted = started, release, interrupted
        self.checkpoints = []

    async def interrupt(self):
        self.interrupted.set()

    async def run_turn(self, prompt, *, on_tool, on_usage, on_started):
        turn_id = uuid4().hex
        await on_started(turn_id)
        await on_usage({"totalTokens": 20})
        self.started.set()
        done, _ = await asyncio.wait([asyncio.ensure_future(self.release.wait()),
                                      asyncio.ensure_future(self.interrupted.wait())],
                                     return_when=asyncio.FIRST_COMPLETED)
        if self.interrupted.is_set():
            await self.close()
            return TurnOutcome(turn_id, "interrupted", True, True)
        result = await on_tool("taskhub_call", {"operation": "checkpoint", "arguments": {
            "checkpoint": {"summary": "Finished after the pause", "next_action": "Continue when resumed"}}}, uuid4().hex)
        self.checkpoints.append(result)
        if getattr(result, "after_send", None) is not None:
            await result.after_send()
        await self.close()
        return TurnOutcome(turn_id, "completed", True, True, visible_output="Done for now.")


async def drive_with_pause(hub, run, pause, *, then=None):
    started, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    harnesses = []

    def factory():
        harnesses.append(Gate(started, release, interrupted))
        return harnesses[-1]

    running = asyncio.create_task(WorkerRunner(hub, factory).run_until_idle(run["id"]))
    try:
        await asyncio.wait_for(started.wait(), 15)
        worker = next(item for item in await hub.list_workers() if item["active_attempt"])
        await pause(worker)
        await asyncio.sleep(1.2)  # The run watcher checks control twice a second.
        if then is not None:
            await then(worker)
            await asyncio.sleep(1.2)
        release.set()
        result = await asyncio.wait_for(running, 30)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    return worker, result, harnesses, interrupted


async def test_a_running_turn_finishes_after_pause_after_turn_and_nothing_follows(case):
    hub, root, run = case

    async def pause(worker):
        await command(hub, "worker.pause_after_turn", {"worker_id": worker["id"], "run_id": run["id"]})

    worker, result, harnesses, interrupted = await drive_with_pause(hub, run, pause)
    assert not interrupted.is_set()
    assert len(harnesses) == 1 and "error" not in harnesses[0].checkpoints[0]
    finished = result["results"][0]
    assert finished["worker_id"] == worker["id"] and finished["turn_status"] == "completed"
    assert finished.get("stop_reason") is None
    # Nothing follows its turn, not even its first-turn collaboration decision.
    assert await hub.next_assignment(run["id"]) is None
    assert await hub.due_recruitment_intents(run["id"]) == []
    assert (await hub.run(run["id"], worker_id=worker["id"]))["pause_mode"] == "after_turn"
    await command(hub, "worker.resume", {"worker_id": worker["id"], "run_id": run["id"]})
    assert [item["source_worker_id"] for item in await hub.due_recruitment_intents(run["id"])] == [worker["id"]]


async def test_pause_all_after_turn_lets_the_turn_finish_and_starts_nothing(case):
    hub, root, run = case

    async def pause(worker):
        await command(hub, "run.pause_after_turn", {"run_id": run["id"]})

    worker, result, harnesses, interrupted = await drive_with_pause(hub, run, pause)
    assert not interrupted.is_set() and len(harnesses) == 1
    assert result["results"][0]["turn_status"] == "completed"
    assert (await hub.run(run["id"]))["state"] == "paused"
    assert await hub.next_assignment(run["id"]) is None
    assert await hub.due_recruitment_intents(run["id"]) == []
    await command(hub, "run.resume", {"run_id": run["id"]})
    assert [item["source_worker_id"] for item in await hub.due_recruitment_intents(run["id"])] == [worker["id"]]


async def test_pause_now_still_interrupts_a_turn_finishing_after_pause_after_turn(case):
    hub, root, run = case

    async def pause(worker):
        await command(hub, "worker.pause_after_turn", {"worker_id": worker["id"], "run_id": run["id"]})

    async def now(worker):
        await command(hub, "worker.pause", {"worker_id": worker["id"], "run_id": run["id"]})

    worker, result, _, interrupted = await drive_with_pause(hub, run, pause, then=now)
    assert interrupted.is_set()
    parked = result["results"][0]
    assert (parked["directive"], parked["stop_reason"]) == ("parked", "operator_pause")


async def test_a_helper_turn_finishes_under_pause_after_turn_and_its_decision_waits(runtime):
    hub, run = runtime
    intent = await helper_intent(hub, run)

    class Deciding(ScriptedHarness):
        async def interrupt(self):
            raise AssertionError("Pause after turn does not interrupt a helper turn")

        async def run_turn(self, prompt, *, on_tool, on_usage, on_started):
            turn_id = uuid4().hex
            await on_started(turn_id)
            await command(hub, "run.pause_after_turn", {"run_id": run["id"]})
            await asyncio.sleep(0.6)  # A helper checks control every 0.2 s.
            await self.close()
            return TurnOutcome(turn_id, "completed", True, True, visible_output=DECISION, final_output=DECISION)

    result = await WorkerRunner(hub, Deciding).run_helper(intent)
    assert result == {"helper_id": intent["id"], "directive": "helper_deferred"}
    saved = await hub.db.call(lambda c: hub._helper(c, intent["id"]))
    assert saved["state"] == "running" and saved["raw_result_json"] and not saved["inference_uncertain"]
    assert await hub.due_assignment_intents(run["id"]) == []  # Nothing is committed while paused.
    await command(hub, "run.resume", {"run_id": run["id"]})
    due = await hub.due_assignment_intents(run["id"])
    assert [item["id"] for item in due] == [intent["id"]]

    class NoInference(ScriptedHarness):
        async def run_turn(self, *args, **kwargs):
            raise AssertionError("A saved decision is committed without another model turn")

    committed = await WorkerRunner(hub, NoInference).run_helper(due[0])
    assert committed["directive"] == "assigned" and committed["outcome"] == "new_worker"
    assert (await hub.db.call(lambda c: hub._helper(c, intent["id"])))["state"] == "completed"


def test_the_api_and_overview_carry_pause_after_turn(tmp_path):
    initialize(tmp_path, ProjectConfig())

    class Idle:
        def __init__(self, hub):
            self.hub = hub

        async def run_until_idle(self, run_id, **_):
            return {"state": "idle"}

    def post(client, name, payload=None):
        response = client.post("/v1/commands", json={"name": name, "key": uuid4().hex, "payload": payload or {}})
        assert response.status_code == 200, response.text
        return response.json()

    with TestClient(create_app(tmp_path, runner_factory=Idle)) as client:
        post(client, "task.publish", {"contract": {"title": "Task 0", "objective": "Satisfy the original goal."}})
        run = post(client, "run.start")
        worker = post(client, "worker.register")
        paused = post(client, "worker.pause_after_turn", {"worker_id": worker["id"], "run_id": run["id"]})
        assert (paused["control"], paused["pause_mode"]) == ("paused", "after_turn")
        listed = next(item for item in client.get("/v1/overview").json()["workers"] if item["id"] == worker["id"])
        assert listed["run_control"] == "paused" and listed["run_control_after_turn"]
        post(client, "run.pause_after_turn", {"run_id": run["id"]})
        overview = client.get("/v1/overview").json()
        assert (overview["runs"][0]["state"], overview["runs"][0]["pause_after_turn"]) == ("paused", 1)
        assert not any(item["run_control"] for item in overview["workers"])  # Pause all ends individual controls.
