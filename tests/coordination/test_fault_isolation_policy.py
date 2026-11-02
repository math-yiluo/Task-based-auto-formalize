"""An engineering fault may quarantine its owner, never the entire run."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import ScriptedHarness, WorkerRunner
from tests.coordination.lifecycle_helpers import assignment_decision, idle_worker


async def execute(hub, name, payload=None, *, runner=False):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             Actor(role=Role.RUNNER if runner else Role.OPERATOR))


async def authorized(hub, attempt):
    """The attempt still holds execution authority."""
    return await hub.authorize_worker(Actor(role=Role.WORKER, worker_id=attempt["worker_id"],
                                            attempt_id=attempt["id"], fencing_token=attempt["fencing_token"]))


@pytest.fixture
async def peers(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    run = await execute(hub, "run.start")
    attempts = []
    for number in range(2):
        task = await execute(hub, "task.publish", {"contract": {
            "title": f"Independent task {number}", "objective": "Exercise fault isolation."}})
        worker = await execute(hub, "worker.register")
        attempts.append(await execute(hub, "task.claim", {"task_id": task["id"], "revision": 1,
            "worker_id": worker["id"], "run_id": run["id"]}, runner=True))
    try:
        yield hub, run, attempts
    finally:
        await hub.close()


@pytest.mark.parametrize("fault", ["unknown_turn", "usage_regression", "native_validation", "preflight"])
async def test_worker_fault_preserves_peer_authority_and_admission(peers, fault):
    hub, run, (broken, healthy) = peers
    if fault == "unknown_turn":
        intent = await execute(hub, "turn.reserve", {"worker_id": broken["worker_id"]}, runner=True)
        await execute(hub, "turn.finish", {"intent_id": intent["id"], "status": "unknown",
            "terminal_observed": False, "process_quiet": True}, runner=True)
    elif fault == "usage_regression":
        for total in (100, 90):
            await execute(hub, "usage.record", {"worker_id": broken["worker_id"],
                "sample": {"totalTokens": total}}, runner=True)
    elif fault == "native_validation":
        await hub.db.call(lambda c: hub._hold_native_validation(c, broken["worker_id"],
                         "Synthetic checker interruption"), write=True)
    else:
        await execute(hub, "runtime.preflight_hold", {"worker_id": broken["worker_id"],
            "attempt_id": broken["id"], "reason": "Synthetic preparation error"}, runner=True)
    state = await hub.run(run["id"])
    assert state["state"] == "running" and state["stop_reason"] is None
    assert (await hub.worker(broken["worker_id"]))["state"] == (
        "recovery_required" if fault == "preflight" else "running")
    await authorized(hub, healthy)
    intent = await execute(hub, "turn.reserve", {"worker_id": healthy["worker_id"]}, runner=True)
    assert intent["state"] == "prepared"
    if fault in {"unknown_turn", "usage_regression"}:
        await authorized(hub, broken)
        with pytest.raises(HubError):
            await execute(hub, "worker.recover", {"worker_id": broken["worker_id"],
                "reason": "Cannot waive unknown paid work", "process_confirmed_stopped": True})


async def test_operator_pause_still_stops_healthy_work_despite_unrelated_fault(peers):
    hub, run, (broken, healthy) = peers
    await hub.db.call(lambda c: hub._hold_worker_usage(c, broken["worker_id"], "Unknown usage"), write=True)
    await execute(hub, "run.pause", {"run_id": run["id"]})
    with pytest.raises(HubError, match="Run no longer"):
        await authorized(hub, healthy)
    await execute(hub, "run.resume", {"run_id": run["id"]})
    await authorized(hub, healthy)
    assert (await hub.run(run["id"]))["usage_uncertain"] == 1


async def test_unknown_helper_does_not_block_unrelated_worker(peers):
    hub, run, (_, healthy) = peers
    await idle_worker(hub, run, "Earlier task")
    await execute(hub, "task.publish", {"contract": {"title": "Available work", "objective": "Synthetic."}})
    helper = await assignment_decision(hub, run["id"])
    await hub.helper_uncertain(helper["id"], "Transport outcome is unknown")
    assert (await hub.run(run["id"]))["state"] == "running"
    await authorized(hub, healthy)
    # The uncertain decision is not replayed, and no second decision replaces it.
    assert await hub.due_assignment_intents(run["id"]) == []
    assert await hub.next_assignment(run["id"]) is None
    decisions = await hub.db.call(lambda c: [tuple(row) for row in c.execute(
        "SELECT id,state FROM helper_intents WHERE kind='assignment'")])
    assert decisions == [(helper["id"], "uncertain")]


async def test_failed_local_recovery_does_not_rollback_or_block_peer_recovery(peers, monkeypatch):
    hub, run, (broken, healthy) = peers
    for attempt in (broken, healthy):
        await execute(hub, "runtime.preflight_hold", {"worker_id": attempt["worker_id"],
            "attempt_id": attempt["id"], "reason": "Temporary preparation failure before dispatch"}, runner=True)
    now = hub.clock()
    monkeypatch.setattr(hub, "clock", lambda: now + 61000)
    original = hub._lifecycle_command

    def fail_one_recovery(c, name, payload, actor):
        result = original(c, name, payload, actor)
        if name == "worker.recover" and payload["worker_id"] == broken["worker_id"]:
            raise OSError("Synthetic failure after a partial recovery write")
        return result

    monkeypatch.setattr(hub, "_lifecycle_command", fail_one_recovery)
    assert await hub.recover_quiet_runtime_failures(run["id"]) == [healthy["worker_id"]]
    assert (await hub.worker(broken["worker_id"]))["state"] == "recovery_required"
    assert (await hub.worker(healthy["worker_id"]))["state"] == "idle"
    assert (await hub.run(run["id"]))["state"] == "running"
    monkeypatch.setattr(hub, "_lifecycle_command", original)
    assert await hub.recover_quiet_runtime_failures(run["id"]) == [broken["worker_id"]]


async def test_restart_quarantines_old_execution_without_changing_run_control(peers):
    hub, run, attempts = peers
    await hub.close()
    reopened = Hub(hub.root)
    try:
        assert (await reopened.run(run["id"]))["state"] == "running"
        for attempt in attempts:
            assert (await reopened.worker(attempt["worker_id"]))["state"] == "recovery_required"
        task = await execute(reopened, "task.publish", {"contract": {
            "title": "Independent new work", "objective": "An unrelated task still admits."}})
        worker = await execute(reopened, "worker.register")
        selected = await execute(reopened, "task.claim", {"task_id": task["id"], "revision": 1,
            "worker_id": worker["id"], "run_id": run["id"]}, runner=True)
        assert selected["state"] == "running"
    finally:
        await reopened.close()


async def test_admission_error_does_not_cancel_an_already_running_peer(peers, monkeypatch):
    hub, run, (_, healthy) = peers
    runner = WorkerRunner(hub, ScriptedHarness)
    calls, completed = [], []

    async def select(_run_id):
        calls.append(1)
        if len(calls) == 1:
            return healthy
        if len(calls) == 2:
            raise OSError("Synthetic admission metadata failure")
        return None

    async def activation(_run_id, *, assignment):
        await asyncio.sleep(0.1)
        await authorized(hub, assignment)
        completed.append(assignment["worker_id"])
        return {"worker_id": assignment["worker_id"], "turn_status": "completed"}

    monkeypatch.setattr(runner, "_select", select)
    monkeypatch.setattr(runner, "run_once", activation)
    result = await runner.run_until_idle(run["id"])
    assert completed == [healthy["worker_id"]]
    assert result["failure_observed"] is True
    assert (await hub.run(run["id"]))["state"] == "running"
