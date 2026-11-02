"""A drained contribution keeps its original check without another model turn."""
from __future__ import annotations

import json

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Role, ValidationReport
from formal_geyser.coordination.plugins import GeneralPlugin
from formal_geyser.coordination.runtime import WorkerRunner
from tests.coordination.test_native_runtime import call, command
from tests.coordination.test_native_runtime import native_runtime as native_runtime
from tests.coordination.test_submission_recovery import prepare


async def contribute(native_runtime):
    case, attempt, runner, source, args = await prepare(native_runtime, "contribute")
    reply = await call(runner, attempt, "author", "contribute", args, "contribution")
    assert reply["runtime_directive"] == "drain_for_submission"
    frozen = await command(case.hub, "runtime.quiescent", {
        "worker_id": attempt["worker_id"],
        "snapshot": case.hub.blobs.snapshot(case.hub.workspace(attempt["worker_id"])),
    }, Actor(role=Role.RUNNER))
    return case, attempt, runner, source, args, reply, frozen["artifact_id"]


async def test_quiet_contribution_recovers_after_restart_without_model_or_new_candidate(native_runtime, monkeypatch):
    case, attempt, _, source, args, reply, aid = await contribute(native_runtime)
    phase = await case.hub.native_validation(aid)
    assert phase["validation_state"] == "pending"
    assert json.loads(phase["response_json"]) == reply
    source.unlink()
    await case.hub.close()
    hub = Hub(case.root)
    try:
        def no_model():
            raise AssertionError("Recovery must not dispatch inference")
        runner = WorkerRunner(hub, no_model)
        async def no_assignment(*_):
            return None
        async def no_helpers(*_):
            return []
        monkeypatch.setattr(runner, "_select", no_assignment)
        monkeypatch.setattr(hub, "due_recruitment_intents", no_helpers)
        monkeypatch.setattr(hub, "due_assignment_intents", no_helpers)
        await runner.run_until_idle(case.run["id"])
        phase = await hub.native_validation(aid)
        assert phase["validation_state"] == "completed"
        assert json.loads(phase["response_json"]) == reply
        assert await call(runner, attempt, "author", "contribute", args, "contribution") == reply
        assert len((await hub.artifact(aid))["evidence"]) == 1
        assert await hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 1
    finally:
        await hub.close()


@pytest.mark.parametrize("saved_verdict", [None, "accepted", "rejected", "unavailable"])
async def test_explicit_recovery_restores_missing_contribution_phase_and_preserves_verdict(native_runtime, saved_verdict):
    case, attempt, runner, source, _, reply, aid = await contribute(native_runtime)
    pending = {"task_id": attempt["task_id"], "task_revision": attempt["task_revision"], "validation_artifact_id": aid}
    if saved_verdict:
        class KnownPlugin(GeneralPlugin):
            async def validate_candidate(self, *args):
                return ValidationReport(verdict=saved_verdict, policy="general.json@1", details={"scope": "Exact synthetic check"})
        runner.plugin = KnownPlugin()
        await runner._recover_submission(pending)
    await case.hub.db.call(lambda c: c.execute(
        "UPDATE native_call_receipts SET validation_artifact_id=NULL,validation_state=NULL,"
        "validation_report_json=NULL,validation_result_json=NULL WHERE call_id='contribution'"), write=True)
    source.unlink()
    with pytest.raises(HubError, match="Confirm"):
        await command(case.hub, "validation.recover", {"artifact_id": aid, "reason": "No process proof"})
    assert await case.hub.native_validation(aid) is None
    recovered = await command(case.hub, "validation.recover", {"artifact_id": aid,
        "process_confirmed_stopped": True, "reason": "The exact stopped synthetic contribution is retained."})
    assert recovered["validation_state"] == "pending"
    if saved_verdict:
        class NoRepeatPlugin(GeneralPlugin):
            async def validate_candidate(self, *args):
                raise AssertionError("A recorded verdict must not be rerun or upgraded")
        runner.plugin = NoRepeatPlugin()
    result = await runner._recover_submission(pending)
    assert result["evidence"][-1]["verdict"] == (saved_verdict or "accepted")
    assert len(result["evidence"]) == 1
    assert json.loads((await case.hub.native_validation(aid))["response_json"]) == reply


async def test_missing_quiet_receipt_cannot_be_reconstructed_from_similar_content(native_runtime):
    case, _, _, _, _, _, aid = await contribute(native_runtime)
    def erase(c):
        c.execute("UPDATE native_call_receipts SET validation_artifact_id=NULL,validation_state=NULL WHERE call_id='contribution'")
        c.execute("DELETE FROM receipts WHERE command='runtime.quiescent'")
    await case.hub.db.call(erase, write=True)
    with pytest.raises(HubError, match="exact quiet contribution"):
        await command(case.hub, "validation.recover", {"artifact_id": aid,
            "process_confirmed_stopped": True, "reason": "No matching quiet receipt remains."})
    assert await case.hub.native_validation(aid) is None


async def test_ambiguous_native_identity_holds_candidate_without_rolling_back_quiescence(native_runtime):
    case, attempt, _, _, _, _ = await _ambiguous_contribution(native_runtime)
    quiet = await command(case.hub, "runtime.quiescent", {
        "worker_id": attempt["worker_id"],
        "snapshot": case.hub.blobs.snapshot(case.hub.workspace(attempt["worker_id"])),
    }, Actor(role=Role.RUNNER))
    assert quiet["directive"] == "validation_held"
    assert quiet["validation_pending"] is True
    assert (await case.hub.artifact(quiet["artifact_id"]))["status"] == "staged"
    assert await case.hub.native_validation(quiet["artifact_id"]) is None
    worker = await case.hub.worker(attempt["worker_id"])
    assert worker["active_attempt"] is None
    assert worker["state"] != "recovery_required"
    ended = await case.hub.db.call(lambda c: dict(c.execute(
        "SELECT * FROM attempts WHERE id=?", (attempt["id"],)).fetchone()))
    assert ended["state"] == "submitted" and ended["ended_at"] is not None


async def _ambiguous_contribution(native_runtime):
    case, attempt, runner, source, args = await prepare(native_runtime, "contribute")
    reply = await call(runner, attempt, "author", "contribute", args, "contribution")
    def duplicate(c):
        row = dict(c.execute("SELECT * FROM native_call_receipts WHERE call_id='contribution'").fetchone())
        row["call_id"] = "conflicting"
        c.execute("INSERT INTO native_call_receipts (" + ",".join(row) + ") VALUES ("
                  + ",".join("?" for _ in row) + ")", tuple(row.values()))
    await case.hub.db.call(duplicate, write=True)
    return case, attempt, runner, source, args, reply


async def test_pre_fix_repair_uses_execution_identity_not_payload_hash(native_runtime):
    case, _, runner, _, _, reply, aid = await contribute(native_runtime)
    def normalized(c):
        row = c.execute("SELECT command_json FROM native_call_receipts WHERE call_id='contribution'").fetchone()
        request = json.loads(row[0])
        request["payload"]["data"]["boundary_filled_field"] = "differs from submitted payload"
        c.execute("UPDATE native_call_receipts SET command_json=?,validation_artifact_id=NULL,validation_state=NULL "
                  "WHERE call_id='contribution'", (json.dumps(request),))
    await case.hub.db.call(normalized, write=True)
    result = await command(case.hub, "validation.recover", {"artifact_id": aid,
        "process_confirmed_stopped": True, "reason": "Repair an exact stopped pre-fix contribution."})
    assert result["validation_state"] == "pending"
    phase = await case.hub.native_validation(aid)
    assert phase["preserve_reply"] == 1
    result = await runner._recover_submission({"task_id": (await case.hub.artifact(aid))["task_id"],
        "task_revision": 1, "validation_artifact_id": aid})
    assert result["evidence"]
    assert json.loads((await case.hub.native_validation(aid))["response_json"]) == reply


async def test_repair_never_substitutes_another_attempt_for_the_quiet_receipt(native_runtime):
    case, _, _, _, _, _, aid = await contribute(native_runtime)
    def change(c):
        c.execute("UPDATE native_call_receipts SET validation_artifact_id=NULL,validation_state=NULL WHERE call_id='contribution'")
        c.execute("UPDATE receipts SET response_json=json_set(response_json,'$.attempt_id','different-execution') "
                  "WHERE command='runtime.quiescent'")
    await case.hub.db.call(change, write=True)
    with pytest.raises(HubError, match="one exact committed contribution"):
        await command(case.hub, "validation.recover", {"artifact_id": aid,
            "process_confirmed_stopped": True, "reason": "A different execution must not be substituted."})
    assert await case.hub.native_validation(aid) is None
