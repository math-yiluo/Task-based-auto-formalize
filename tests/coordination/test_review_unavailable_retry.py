"""Unavailable retries append real local evidence without replaying model calls."""
import asyncio
import json

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.gateway_receipts import RETRY_BASE_MS
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Role, ValidationReport
from formal_geyser.coordination.plugins import GeneralPlugin
from tests.coordination.test_contribution_validation_recovery import (
    _ambiguous_contribution,
    contribute,
)
from tests.coordination.test_native_runtime import command
from tests.coordination.test_native_runtime import native_runtime as native_runtime


class ResultPlugin(GeneralPlugin):
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    async def validate_candidate(self, *args):
        self.calls += 1
        return ValidationReport(verdict=self.verdict, policy="general.json@1", details={"reason": "Synthetic actual result"})


async def test_unavailable_recovers_automatically_and_preserves_wire_reply(native_runtime):
    case, attempt, runner, _, _, reply, aid = await contribute(native_runtime)
    now = [case.hub.clock()]
    case.hub.clock = lambda: now[0]
    pending = {"task_id": attempt["task_id"], "task_revision": 1, "validation_artifact_id": aid}
    runner.plugin = ResultPlugin("unavailable")
    original = await runner._recover_submission(pending)
    assert original["evidence"][-1]["verdict"] == "unavailable"
    waiting = await case.hub.db.call(lambda c: case.hub._validation_queue_status(c, aid))
    assert not waiting["validation_ready"] and waiting["validation_retry_at"] == now[0] + RETRY_BASE_MS
    assert not await case.hub.pending_native_validations(case.run["id"])
    now[0] += RETRY_BASE_MS
    queued = await case.hub.pending_native_validations(case.run["id"])
    assert [row["validation_artifact_id"] for row in queued] == [aid]
    runner.plugin = ResultPlugin("accepted")
    result = await runner._recover_submission(queued[0])
    assert [e["verdict"] for e in result["evidence"]] == ["accepted"]
    assert [e["verdict"] for e in (await case.hub.artifact(aid))["evidence"]] == ["unavailable", "accepted"]
    assert runner.plugin.calls == 1
    assert (await case.hub.db.call(lambda c: case.hub._validation_queue_status(c, aid)))["validation_ready"]
    phase = await case.hub.native_validation(aid)
    assert phase["validation_generation"] == 1 and phase["validation_retry_at"] is None
    assert json.loads(phase["response_json"]) == reply
    await case.hub.db.call(lambda c: case.hub._require_original_validation(c, case.hub._candidate_binding(c, aid)))


@pytest.mark.parametrize("verdict", ["rejected", "needs_operator", "accepted"])
async def test_findings_and_operator_decisions_never_retry(native_runtime, verdict):
    case, attempt, runner, _, _, _, aid = await contribute(native_runtime)
    runner.plugin = ResultPlugin(verdict)
    await runner._recover_submission({"task_id": attempt["task_id"], "task_revision": 1, "validation_artifact_id": aid})
    now = case.hub.clock()
    case.hub.clock = lambda: now + 10 * RETRY_BASE_MS
    assert not await case.hub.pending_native_validations(case.run["id"])
    with pytest.raises(HubError, match="not unavailable"):
        await command(case.hub, "validation.retry", {"artifact_id": aid, "reason": "Must not erase this result"})


async def test_backoff_and_manual_pause_prevent_immediate_reruns(native_runtime):
    case, attempt, runner, _, _, _, aid = await contribute(native_runtime)
    now = [case.hub.clock()]
    case.hub.clock = lambda: now[0]
    runner.plugin = ResultPlugin("unavailable")
    pending = {"task_id": attempt["task_id"], "task_revision": 1, "validation_artifact_id": aid}
    await runner._recover_submission(pending)
    now[0] += RETRY_BASE_MS
    await command(case.hub, "run.pause", {"run_id": case.run["id"]})
    with pytest.raises(HubError, match="Run is not active"):
        await case.hub.pending_native_validations(case.run["id"])
    assert (await case.hub.native_validation(aid))["validation_generation"] == 0
    await command(case.hub, "run.resume", {"run_id": case.run["id"]})
    await case.hub.pending_native_validations(case.run["id"])
    await runner._recover_submission(pending)
    phase = await case.hub.native_validation(aid)
    assert phase["validation_retry_at"] == now[0] + 2 * RETRY_BASE_MS
    assert len((await case.hub.artifact(aid))["evidence"]) == 2


async def test_operator_can_bind_only_the_exact_quiet_native_call(native_runtime):
    case, attempt, _, _, _, _ = await _ambiguous_contribution(native_runtime)
    quiet = await command(case.hub, "runtime.quiescent", {
        "worker_id": attempt["worker_id"], "snapshot": case.hub.blobs.snapshot(case.hub.workspace(attempt["worker_id"]))
    }, Actor(role=Role.RUNNER))
    aid = quiet["artifact_id"]
    payload = {"artifact_id": aid, "process_confirmed_stopped": True,
               "reason": "Exact local history identifies the original call"}
    with pytest.raises(HubError, match="one exact"):
        await command(case.hub, "validation.recover", payload)
    with pytest.raises(HubError, match="one exact"):
        await command(case.hub, "validation.recover", {**payload, "native_call": {
            "worker_id": attempt["worker_id"], "thread_id": "other-thread", "call_id": "contribution"}})
    result = await command(case.hub, "validation.recover", {**payload, "native_call": {
        "worker_id": attempt["worker_id"], "thread_id": "author", "call_id": "contribution"}})
    assert result["validation_state"] == "pending"
    phase = await case.hub.native_validation(aid)
    assert phase["call_id"] == "contribution"


async def test_unconfirmed_process_cleanup_never_autoretries(native_runtime):
    case, attempt, runner, _, _, _, aid = await contribute(native_runtime)
    class Unquiet(GeneralPlugin):
        async def validate_candidate(self, *args):
            return ValidationReport(verdict="unavailable", policy="general.json@1",
                                    details={"compilation": {"process": {"tree_quiet": False}}})
    runner.plugin = Unquiet()
    await runner._recover_submission({"task_id": attempt["task_id"], "task_revision": 1, "validation_artifact_id": aid})
    phase = await case.hub.native_validation(aid)
    assert phase["validation_state"] == "uncertain" and phase["validation_retry_at"] is None
    with pytest.raises(HubError):
        await command(case.hub, "validation.retry", {"artifact_id": aid, "reason": "Cannot guess process death"})
    await command(case.hub, "validation.recover", {"artifact_id": aid,
                  "process_confirmed_stopped": True, "reason": "Synthetic checker is now confirmed stopped"})
    assert (await case.hub.native_validation(aid))["validation_generation"] == 1


async def test_retry_schedule_survives_restart(native_runtime):
    case, attempt, runner, _, _, _, aid = await contribute(native_runtime)
    now = [case.hub.clock()]
    case.hub.clock = lambda: now[0]
    runner.plugin = ResultPlugin("unavailable")
    await runner._recover_submission({"task_id": attempt["task_id"], "task_revision": 1, "validation_artifact_id": aid})
    due = (await case.hub.native_validation(aid))["validation_retry_at"]
    await case.hub.close()
    restarted = Hub(case.root, clock=lambda: now[0])
    try:
        assert (await restarted.native_validation(aid))["validation_retry_at"] == due
        now[0] = due
        assert [r["validation_artifact_id"] for r in await restarted.pending_native_validations(case.run["id"])] == [aid]
    finally:
        await restarted.close()


@pytest.mark.parametrize("advances_generation", [False, True])
async def test_driver_attempts_each_generation_once_without_inference(native_runtime, monkeypatch, advances_generation):
    case, _, runner, _, _, _, aid = await contribute(native_runtime)
    now = [case.hub.clock()]
    case.hub.clock = lambda: now[0]
    async def no_work(*args):
        return None
    async def no_helpers(*args):
        return []
    monkeypatch.setattr(runner, "_select", no_work)
    monkeypatch.setattr(case.hub, "due_recruitment_intents", no_helpers)
    monkeypatch.setattr(case.hub, "due_assignment_intents", no_helpers)
    calls = []
    original = runner._recover_submission
    async def recover(pending, **options):
        calls.append(pending["validation_generation"])
        if not advances_generation:
            return {"validation_pending": True}  # Same local phase still cannot finish.
        runner.plugin = ResultPlugin("unavailable" if len(calls) == 1 else "accepted")
        result = await original(pending, **options)
        if len(calls) == 1:
            now[0] += RETRY_BASE_MS
        return result
    monkeypatch.setattr(runner, "_recover_submission", recover)
    await asyncio.wait_for(runner.run_until_idle(case.run["id"]), 30)
    assert calls == ([0, 1] if advances_generation else [0])
    if advances_generation:
        assert [e["verdict"] for e in (await case.hub.artifact(aid))["evidence"]] == ["unavailable", "accepted"]
