"""Exact post-staging validation recovery without model calls or duplicate candidates."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import ValidationReport
from formal_geyser.coordination.plugins import GeneralPlugin
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.process import OwnedProcessHost
from formal_geyser.process import ProcessRunner
from tests.coordination.lifecycle_helpers import select_review
from tests.coordination.publication_helpers import publish_batch
from tests.coordination.test_native_runtime import call, claim, command, write
from tests.coordination.test_native_runtime import native_runtime as native_runtime


async def prepare(native_runtime, operation="stage", *, packet_bytes=32768):
    case = await native_runtime(repository=True, packet_bytes=packet_bytes)
    attempt = await claim(case)
    workspace = case.hub.workspace(attempt["worker_id"])
    if operation == "wiki_submit":
        source = write(workspace, "wiki/new.md", "# Exact submitted note\n")
        args = {"paths": ["wiki/new.md"], "summary": "A useful synthetic note."}
    else:
        write(workspace, "document.md", "# Exact submitted document\n")
        source = write(workspace, "submission.json", json.dumps({"kind": "json", "data": {"ready": True},
            "repository_paths": ["document.md"], "introduction": "Inspect the exact synthetic source."}))
        args = {"manifest_path": "submission.json"}
    return case, attempt, WorkerRunner(case.hub), source, args


async def lose_before_validation(runner, attempt, operation, args, monkeypatch):
    original = runner._validate_submission

    async def lost(*_, **__):
        raise OSError("Lost before original validation was started")

    monkeypatch.setattr(runner, "_validate_submission", lost)
    with pytest.raises(OSError):
        await call(runner, attempt, "author", operation, args, "same-call")
    monkeypatch.setattr(runner, "_validate_submission", original)
    return await runner.hub.db.call(lambda c: dict(c.execute(
        "SELECT * FROM native_call_receipts WHERE call_id='same-call'").fetchone()))


async def publish(case, runner, staged):
    reviewer = await select_review(case.hub, staged["review_task_id"], case.run["id"])
    basis = reviewer["task"]["collaboration"]["review"]
    decision = {key: basis[key] for key in (
        "candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")}
    decision.update(verdict="accept", rationale="Compared exact synthetic source.", evidence=["document.md:1"])
    await call(runner, reviewer, "reviewer", "review", decision, "accept")
    await case.hub.ordinary_turn_completed(reviewer["worker_id"], reviewer["id"])
    await publish_batch(case.hub, case.run["id"])
    assert (await case.hub.artifact(staged["artifact_id"]))["public"] is True


@pytest.mark.parametrize("operation", ["stage", "wiki_submit"])
async def test_exact_replay_finishes_missing_validation_and_publishes_original(native_runtime, monkeypatch, operation):
    case, attempt, runner, source, args = await prepare(native_runtime, operation)
    row = await lose_before_validation(runner, attempt, operation, args, monkeypatch)
    assert row["validation_state"] == "pending" and row["response_json"] is None
    source.unlink()  # The immutable staged files, not mutable request files, are authoritative.
    recovered = await call(WorkerRunner(case.hub), attempt, "author", operation, args, "same-call")
    assert recovered["artifact_id"] == row["validation_artifact_id"]
    assert recovered["validation"]["evidence"][-1]["verdict"] == "accepted"
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 1
    await publish(case, runner, recovered)


async def test_pause_retains_missing_phase_without_caching_partial_reply(native_runtime, monkeypatch):
    case, attempt, runner, _, args = await prepare(native_runtime)
    await lose_before_validation(runner, attempt, "stage", args, monkeypatch)
    await command(case.hub, "run.pause", {"run_id": case.run["id"]})
    pending = await call(runner, attempt, "author", "stage", args, "same-call")
    assert pending["validation"]["code"] == "RUN_PAUSED"
    row = await case.hub.native_validation(pending["artifact_id"])
    assert row["validation_state"] == "pending" and row["response_json"] is None
    assert (await case.hub.artifact(pending["artifact_id"]))["evidence"] == []
    await command(case.hub, "run.resume", {"run_id": case.run["id"]})
    completed = await call(runner, attempt, "author", "stage", args, "same-call")
    assert len(completed["validation"]["evidence"]) == 1
    await command(case.hub, "run.pause", {"run_id": case.run["id"]})
    assert await call(runner, attempt, "author", "stage", args, "same-call") == completed


async def test_completed_bounded_reply_replays_without_source_or_validation_reads(native_runtime, monkeypatch):
    case, attempt, runner, source, args = await prepare(native_runtime, packet_bytes=2048)
    completed = await call(runner, attempt, "author", "stage", args, "same-call")
    assert completed["requires_read"] is True and "validation" not in completed
    source.unlink()

    async def no_recovery(*_):
        raise AssertionError("A finished bounded reply must replay exactly")

    monkeypatch.setattr(runner, "_recover_submission", no_recovery)
    await command(case.hub, "run.pause", {"run_id": case.run["id"]})
    assert await call(runner, attempt, "author", "stage", args, "same-call") == completed


@pytest.mark.parametrize("verdict", ["rejected", "unavailable"])
async def test_saved_negative_validation_is_preserved_after_response_loss(native_runtime, monkeypatch, verdict):
    case, attempt, runner, source, args = await prepare(native_runtime)
    validations = []

    class NegativePlugin(GeneralPlugin):
        async def validate_candidate(self, hub, task, artifact):
            validations.append(artifact["id"])
            return ValidationReport(verdict=verdict, policy="general.json@1", details={"reason": "Synthetic result"})

    runner.plugin = NegativePlugin()
    original = runner._validate_submission

    async def lost(*values, **options):
        await original(*values, **options)
        raise OSError("Reply lost after actual validation was saved")

    monkeypatch.setattr(runner, "_validate_submission", lost)
    with pytest.raises(OSError):
        await call(runner, attempt, "author", "stage", args, "same-call")
    source.unlink()
    await command(case.hub, "run.pause", {"run_id": case.run["id"]})
    recovered = await call(runner, attempt, "author", "stage", args, "same-call")
    assert recovered["validation"]["evidence"][-1]["verdict"] == verdict
    assert validations == [recovered["artifact_id"]]


async def test_prepared_command_replay_also_finishes_original_validation(native_runtime, monkeypatch):
    case, attempt, runner, source, args = await prepare(native_runtime)
    execute = case.hub.execute

    async def interrupted(command, actor=None, *, native_call=None):
        if command.name == "artifact.stage":
            raise OSError("After normalization, before command execution")
        return await execute(command, actor, native_call=native_call)

    monkeypatch.setattr(case.hub, "execute", interrupted)
    with pytest.raises(OSError):
        await call(runner, attempt, "author", "stage", args, "same-call")
    row = await case.hub.db.call(lambda c: dict(c.execute("SELECT * FROM native_call_receipts").fetchone()))
    assert row["command_json"] and row["core_response_json"] is None
    source.write_text("invalid changed manifest", encoding="utf-8")
    monkeypatch.setattr(case.hub, "execute", execute)
    result = await call(runner, attempt, "author", "stage", args, "same-call")
    assert result["validation"]["evidence"][-1]["verdict"] == "accepted"


@pytest.mark.parametrize("verdict", ["accepted", "rejected"])
async def test_saved_checker_report_retries_only_its_receipt_write(native_runtime, monkeypatch, verdict):
    case, attempt, runner, source, args = await prepare(native_runtime)
    validations = []
    execute = case.hub.execute

    class FinishedPlugin(GeneralPlugin):
        async def validate_candidate(self, hub, task, artifact):
            validations.append(artifact["id"])
            return ValidationReport(verdict=verdict, policy="general.json@1", details={"scope": "Synthetic check"})

    async def lost_accept(command, actor=None, **kwargs):
        if command.name == "artifact.accept":
            raise OSError("Receipt write interrupted after completed checker report")
        return await execute(command, actor, **kwargs)

    runner.plugin = FinishedPlugin()
    monkeypatch.setattr(case.hub, "execute", lost_accept)
    with pytest.raises(OSError):
        await call(runner, attempt, "author", "stage", args, "same-call")
    monkeypatch.setattr(case.hub, "execute", execute)
    source.unlink()
    result = await call(runner, attempt, "author", "stage", args, "same-call")
    assert result["validation"]["evidence"][-1]["verdict"] == verdict
    assert validations == [result["artifact_id"]]


@pytest.mark.parametrize("change", ["revision", "retirement", "withdrawal"])
async def test_basis_change_after_finished_check_is_not_process_uncertainty(native_runtime, change):
    case, attempt, runner, _, args = await prepare(native_runtime)
    validations = []

    class ChangedBasisPlugin(GeneralPlugin):
        async def validate_candidate(self, hub, task, artifact):
            validations.append(artifact["id"])
            report = await super().validate_candidate(hub, task, artifact)
            if change == "withdrawal":
                await command(hub, "artifact.withdraw", {"artifact_id": artifact["id"], "reason": "Withdraw after check."})
            elif change == "retirement":
                await command(hub, "task.retire", {"task_id": task["id"], "revision": 1, "reason": "Retire after check."})
            else:
                await command(hub, "task.revise", {"task_id": task["id"], "revision": 1,
                    "contract": {**task["contract"], "objective": "Updated after the original check."},
                    "reason": "Revision after completed check."})
            return report

    runner.plugin = ChangedBasisPlugin()
    result = await call(runner, attempt, "author", "stage", args, "same-call")
    assert result["validation"]["state"] == "obsolete"
    row = await case.hub.native_validation(result["artifact_id"])
    assert row["validation_state"] == "obsolete" and row["validation_result_json"] is None
    assert json.loads(row["validation_report_json"])["verdict"] == "accepted"
    assert validations == [result["artifact_id"]]
    assert (await case.hub.run(case.run["id"]))["state"] == "running"
    assert (await case.hub.worker(attempt["worker_id"]))["state"] != "recovery_required"


async def test_existing_driver_boundary_recovers_without_model_replaying_call(native_runtime, monkeypatch):
    case, attempt, runner, source, args = await prepare(native_runtime)
    row = await lose_before_validation(runner, attempt, "stage", args, monkeypatch)
    await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    source.unlink()

    async def no_assignment(*_):
        return None

    async def no_helpers(*_):
        return []

    def no_model():
        raise AssertionError("Missing local validation must not start another model turn")

    runner.harness_factory = no_model
    monkeypatch.setattr(runner, "_select", no_assignment)
    monkeypatch.setattr(case.hub, "due_recruitment_intents", no_helpers)
    monkeypatch.setattr(case.hub, "due_assignment_intents", no_helpers)
    await runner.run_until_idle(case.run["id"])
    saved = await case.hub.native_validation(row["validation_artifact_id"])
    assert saved["validation_state"] == "completed"
    assert len((await case.hub.artifact(row["validation_artifact_id"]))["evidence"]) == 1
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 1


async def test_unknown_validation_process_is_held_until_explicit_recovery(native_runtime):
    case, attempt, runner, _, args = await prepare(native_runtime)
    validations = []

    class UnknownPlugin(GeneralPlugin):
        async def validate_candidate(self, hub, task, artifact):
            validations.append(artifact["id"])
            raise RuntimeError("Synthetic checker outcome not known")

    runner.plugin = UnknownPlugin()
    with pytest.raises(RuntimeError):
        await call(runner, attempt, "author", "stage", args, "same-call")
    repeated = await call(runner, attempt, "author", "stage", args, "same-call")
    assert repeated["validation"]["code"] == "VALIDATION_RECONCILIATION_REQUIRED"
    row = await case.hub.native_validation(repeated["artifact_id"])
    assert row["validation_state"] == "uncertain" and len(validations) == 1
    assert (await case.hub.run(case.run["id"]))["state"] == "running"
    assert (await case.hub.worker(attempt["worker_id"]))["state"] == "running"
    with pytest.raises(HubError):
        await command(case.hub, "validation.recover", {"artifact_id": repeated["artifact_id"]})
    assert (await case.hub.native_validation(repeated["artifact_id"]))["validation_state"] == "uncertain"
    await command(case.hub, "validation.recover", {"artifact_id": repeated["artifact_id"],
        "process_confirmed_stopped": True, "reason": "Synthetic checker process confirmed stopped."})
    assert (await case.hub.native_validation(repeated["artifact_id"]))["validation_state"] == "pending"
    await command(case.hub, "run.resume", {"run_id": case.run["id"]})
    runner.plugin = GeneralPlugin()
    result = await call(runner, attempt, "author", "stage", args, "same-call")
    assert result["validation"]["evidence"][-1]["verdict"] == "accepted"


@pytest.mark.parametrize("state", ["idle", "suspended"])
async def test_validation_only_recovery_preserves_quiet_worker_intention(native_runtime, state):
    case, attempt, runner, _, args = await prepare(native_runtime)

    class UnknownPlugin(GeneralPlugin):
        async def validate_candidate(self, *args):
            raise RuntimeError("Unknown original validation")

    runner.plugin = UnknownPlugin()
    with pytest.raises(RuntimeError):
        await call(runner, attempt, "author", "stage", args, "same-call")
    await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    # Simulate the known native turn's later quiet parking/idle transition.
    await case.hub.db.call(lambda c: c.execute("UPDATE workers SET state=? WHERE id=?",
        (state, attempt["worker_id"])), write=True)
    before = await case.hub.worker(attempt["worker_id"])
    with pytest.raises(HubError):
        await command(case.hub, "worker.recover", {"worker_id": attempt["worker_id"], "reason": "No proof"})
    result = await command(case.hub, "worker.recover", {"worker_id": attempt["worker_id"],
        "process_confirmed_stopped": True, "reason": "All synthetic validation processes stopped."})
    assert result["validation_only"] is True
    after = await case.hub.worker(attempt["worker_id"])
    assert (after["state"], after["work_intention"], after["current_task_id"]) == (
        before["state"], before["work_intention"], before["current_task_id"])
    assert (await case.hub.run(case.run["id"]))["state"] == "running"


async def test_new_run_preflight_finishes_old_candidate_without_old_call_replay(native_runtime, monkeypatch):
    case, attempt, runner, _, args = await prepare(native_runtime)
    row = await lose_before_validation(runner, attempt, "stage", args, monkeypatch)
    await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    await command(case.hub, "run.stop", {"run_id": case.run["id"]})
    later = await command(case.hub, "run.start")
    assignment = await command(case.hub, "task.claim", {"task_id": attempt["task_id"],
        "revision": 1, "worker_id": attempt["worker_id"], "run_id": later["id"]})

    class ResumedHarness(ScriptedHarness):
        async def open(self, **kwargs):
            retained = await case.hub.native_validation(row["validation_artifact_id"])
            assert retained["validation_state"] == "completed"
            return await super().open(**kwargs)

    harness = ResumedHarness([ScriptedTurn([])])
    await WorkerRunner(case.hub, lambda: harness).run_once(later["id"], assignment=assignment)
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 1
    assert len((await case.hub.artifact(row["validation_artifact_id"]))["evidence"]) == 1


async def test_restart_quarantines_running_local_validation(native_runtime, monkeypatch):
    case, attempt, runner, _, args = await prepare(native_runtime)
    row = await lose_before_validation(runner, attempt, "stage", args, monkeypatch)
    await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    await case.hub.native_validation(row["validation_artifact_id"], start=True)
    await case.hub.close()
    reopened = Hub(case.root)
    try:
        retained = await reopened.native_validation(row["validation_artifact_id"])
        assert retained["validation_state"] == "uncertain"
        assert (await reopened.worker(attempt["worker_id"]))["state"] == "idle"
        assert (await reopened.run(case.run["id"]))["state"] == "running"
        with pytest.raises(HubError, match="recovery"):
            await reopened.native_validation(row["validation_artifact_id"], start=True)
    finally:
        await reopened.close()


@pytest.mark.parametrize("change", ["withdrawal", "revision", "retirement"])
async def test_obsolete_original_validation_leaves_recovery_queue_once(native_runtime, monkeypatch, change):
    case, attempt, runner, _, args = await prepare(native_runtime)
    row = await lose_before_validation(runner, attempt, "stage", args, monkeypatch)
    await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    if change == "withdrawal":
        await command(case.hub, "artifact.withdraw", {"artifact_id": row["validation_artifact_id"],
                                                       "reason": "Retire the old synthetic candidate."})
    elif change == "retirement":
        await command(case.hub, "task.retire", {"task_id": attempt["task_id"], "revision": 1,
                                               "reason": "Retire the synthetic task."})
    else:
        await command(case.hub, "task.revise", {"task_id": attempt["task_id"], "revision": 1,
            "contract": {**case.host["contract"], "objective": "A revised synthetic objective."},
            "reason": "Change the exact task basis."})
    result = await runner._recover_submission({"task_id": attempt["task_id"], "task_revision": 1,
                                               "validation_artifact_id": row["validation_artifact_id"]})
    assert result["state"] == "obsolete" and result["code"] == "INPUTS_CHANGED"
    assert await case.hub.pending_native_validations(case.run["id"]) == []
    assert (await case.hub.run(case.run["id"]))["state"] == "running"


async def test_changed_input_basis_holds_once_and_recovery_rechecks_it(native_runtime, monkeypatch):
    case, attempt, runner, _, args = await prepare(native_runtime)
    row = await lose_before_validation(runner, attempt, "stage", args, monkeypatch)
    await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    basis_valid = case.hub._basis_valid
    monkeypatch.setattr(case.hub, "_basis_valid", lambda *args, **kwargs: False)
    pending = {"task_id": attempt["task_id"], "task_revision": 1,
               "validation_artifact_id": row["validation_artifact_id"]}
    result = await runner._recover_submission(pending)
    assert result["state"] == "blocked" and result["code"] == "INPUTS_CHANGED"
    assert await case.hub.pending_native_validations(case.run["id"]) == []
    recovery = {"worker_id": attempt["worker_id"], "process_confirmed_stopped": True,
                "reason": "Inspect the exact restored synthetic basis."}
    await command(case.hub, "worker.recover", recovery)
    assert (await case.hub.native_validation(row["validation_artifact_id"]))["validation_state"] == "blocked"
    monkeypatch.setattr(case.hub, "_basis_valid", basis_valid)
    assert (await case.hub.native_validation(row["validation_artifact_id"]))["validation_state"] == "blocked"
    await command(case.hub, "worker.recover", recovery)
    result = await runner._recover_submission(pending)
    assert result["evidence"][-1]["verdict"] == "accepted"


async def test_pause_during_resumed_validation_awaits_owned_process_cleanup(native_runtime, monkeypatch):
    case, attempt, runner, _, args = await prepare(native_runtime)
    row = await lose_before_validation(runner, attempt, "stage", args, monkeypatch)
    await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    hosts = []
    actual_start = OwnedProcessHost.start
    marker = case.root / "synthetic-checker-started.txt"

    async def capture_host(*args, **kwargs):
        host = await actual_start(*args, **kwargs)
        hosts.append(host)
        return host

    monkeypatch.setattr(OwnedProcessHost, "start", staticmethod(capture_host))

    class ProcessPlugin(GeneralPlugin):
        async def validate_candidate(self, *args):
            code = f"from pathlib import Path; import time; Path({str(marker)!r}).write_text('started'); time.sleep(60)"
            await ProcessRunner().run([sys.executable, "-c", code], cwd=case.root)
            raise AssertionError("Pause must cancel the synthetic checker before it completes")

    runner.plugin = ProcessPlugin()
    pending = {"task_id": attempt["task_id"], "task_revision": 1,
               "validation_artifact_id": row["validation_artifact_id"]}
    running = asyncio.create_task(runner._recover_submission(pending))

    async def started():
        while not marker.exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(started(), timeout=10)
        await command(case.hub, "run.pause", {"run_id": case.run["id"]})
        result = await asyncio.wait_for(running, timeout=10)
        assert result["code"] == "RUN_PAUSED" and result["state"] == "uncertain"
        assert len(hosts) == 1 and hosts[0].tree_quiet
        assert (await case.hub.native_validation(row["validation_artifact_id"]))["validation_state"] == "uncertain"
        assert (await case.hub.artifact(row["validation_artifact_id"]))["evidence"] == []
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


