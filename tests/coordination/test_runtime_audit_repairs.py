"""Cross-transition regressions with local synthetic data and no model calls."""
from __future__ import annotations

import asyncio
import json

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Role
from formal_geyser.coordination.monitoring import SnapshotReader, Watchdog
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.storage import canonical
from tests.coordination.communication_helpers import native_text
from tests.coordination.lifecycle_helpers import assignment_decision, open_collaboration
from tests.coordination.test_native_runtime import call, claim, command, worker, write
from tests.coordination.test_native_runtime import native_runtime as native_runtime

RUNNER = Actor(role=Role.RUNNER)


@pytest.mark.parametrize("closure", ["departure", "retirement"])
async def test_closed_peer_channel_delivers_once_without_new_send_rights(native_runtime, closure):
    case = await native_runtime()
    hub = case.hub
    sender, recipient = await claim(case), await claim(case)
    text = "Retain this already-authorized result after our channel closes."
    sent = await command(hub, "talk.send", {"recipient_id": recipient["worker_id"],
        "message_id": "last-note", "text": text}, worker(sender))
    if closure == "departure":
        await command(hub, "task.leave", {"task_id": recipient["task_id"], "permanent": True,
                                          "reason": "My contribution has ended."}, worker(recipient))
    else:
        await command(hub, "task.retire", {"task_id": recipient["task_id"], "revision": 1,
                                           "reason": "Close this synthetic task."})
    await command(hub, "runtime.quiescent", {"worker_id": recipient["worker_id"],
        "snapshot": hub.blobs.snapshot(hub.workspace(recipient["worker_id"]))}, RUNNER)

    class ReadingHarness(ScriptedHarness):
        async def run_turn(self, prompt, **callbacks):
            packet = json.loads(prompt)
            assert packet["message_state"]["task_execution_authority"] is False
            assert native_text(packet["messages"][0]) == text
            history = await callbacks["on_tool"]("taskhub_call", {
                "operation": "talk_read", "arguments": {"conversation_id": sent["conversation_id"]}}, "history")
            assert native_text(history["messages"][0]) == text
            denied = await callbacks["on_tool"]("taskhub_call", {"operation": "talk_send", "arguments": {
                "recipient_id": sender["worker_id"], "message_id": "new-note", "text": "Not authorized."}}, "denied")
            assert "error" not in denied  # Project contact no longer needs shared task membership.
            return await super().run_turn(prompt, **callbacks)

    harness = ReadingHarness([ScriptedTurn([])])
    result = await WorkerRunner(hub, lambda: harness).run_once(case.run["id"])
    assert result["worker_id"] == recipient["worker_id"] and result["directive"] == "message_handled"
    state = await hub.worker(recipient["worker_id"])
    assert state["pending_wake"] == 0 and state["work_intention"] == "idle"
    assert await hub.db.call(lambda c: c.execute(
        "SELECT acknowledged_at IS NOT NULL FROM peer_messages WHERE id=?", (sent["id"],)).fetchone()[0])
    assert not any(member["worker_id"] == recipient["worker_id"] and member["membership_state"] == "member"
                   for member in (await hub.task(recipient["task_id"]))["collaboration"]["members"])
    next_attempt = await hub.next_assignment(case.run["id"])
    assert next_attempt is None or next_attempt.get("execution_mode") != "message_only"


async def submission(case, attempt):
    workspace = case.hub.workspace(attempt["worker_id"])
    write(workspace, "document.md", "# Exact submitted document\n")
    write(workspace, "submission.json", json.dumps({"kind": "json", "data": {"summary": "Original"},
        "repository_paths": ["document.md"], "input_refs": []}))
    return workspace


@pytest.mark.parametrize("operation", ["contribute", "leave_task", "stage", "wiki_submit"])
async def test_native_call_replays_original_receipt_after_its_inputs_change(native_runtime, operation):
    case = await native_runtime(repository=operation != "leave_task")
    attempt = await claim(case)
    runner = WorkerRunner(case.hub)
    if operation == "leave_task":
        args = {"task_id": attempt["task_id"], "reason": "No useful contribution remains."}
    elif operation == "wiki_submit":
        workspace = case.hub.workspace(attempt["worker_id"])
        target = write(workspace, "wiki/new-page.md", "# A useful public note\n")
        args = {"paths": ["wiki/new-page.md"], "summary": "Useful note."}
    else:
        workspace = await submission(case, attempt)
        args = {"manifest_path": "submission.json"}
    first = await call(runner, attempt, "native-thread", operation, args, "same-call")
    if operation == "stage":
        write(workspace, "submission.json", "not the original manifest")
    elif operation == "wiki_submit":
        target.unlink()
    second = await call(WorkerRunner(case.hub), attempt, "native-thread", operation, args, "same-call")
    assert second == first
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM native_call_receipts").fetchone()[0]) == 1
    if operation in {"stage", "wiki_submit"}:
        assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 1


async def test_native_call_identity_conflicts_and_pause_keep_new_calls_fenced(native_runtime):
    case = await native_runtime(repository=True)
    attempt = await claim(case)
    await submission(case, attempt)
    await command(case.hub, "worker.bind_session", {"worker_id": attempt["worker_id"],
                                                   "thread_id": "native-thread"}, RUNNER)
    runner = WorkerRunner(case.hub)
    args = {"manifest_path": "submission.json"}
    first = await call(runner, attempt, "native-thread", "stage", args, "same-call")
    for changed_operation, changed_arguments in [("stage", {"manifest_path": "changed.json"}),
                                                 ("inspect", {"task_id": attempt["task_id"]})]:
        with pytest.raises(HubError, match="reused with different data"):
            await call(runner, attempt, "native-thread", changed_operation, changed_arguments, "same-call")
    with pytest.raises(HubError) as fence:
        await call(runner, {**attempt, "fencing_token": attempt["fencing_token"] + 1},
                   "native-thread", "stage", args, "same-call")
    assert fence.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(HubError) as thread:
        await call(runner, attempt, "another-thread", "stage", args, "same-call")
    assert thread.value.code == "NATIVE_CALL_IDENTITY"
    other = await claim(case)
    with pytest.raises(HubError) as identity:
        await call(runner, {**attempt, "worker_id": other["worker_id"]},
                   "native-thread", "stage", args, "same-call")
    assert identity.value.code == "STALE_BINDING"
    await command(case.hub, "run.pause", {"run_id": case.run["id"]})
    assert await call(runner, attempt, "native-thread", "stage", args, "same-call") == first
    with pytest.raises(HubError) as paused:
        await call(runner, attempt, "native-thread", "stage", args, "new-call")
    assert paused.value.code == "RUN_PAUSED"


async def test_native_commit_survives_lost_postcommit_response_without_revalidating(native_runtime, monkeypatch):
    case = await native_runtime(repository=True)
    attempt = await claim(case)
    workspace = await submission(case, attempt)
    runner = WorkerRunner(case.hub)
    calls = []
    original = runner._validate_submission

    async def lost_response(*args, **options):
        calls.append(args)
        await original(*args, **options)
        raise OSError("Synthetic lost postcommit response.")

    monkeypatch.setattr(runner, "_validate_submission", lost_response)
    with pytest.raises(OSError):
        await call(runner, attempt, "native-thread", "stage", {"manifest_path": "submission.json"}, "same-call")
    (workspace / "submission.json").unlink()
    result = await call(runner, attempt, "native-thread", "stage", {"manifest_path": "submission.json"}, "same-call")
    assert result["artifact_id"] and len(calls) == 1
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == 1


async def test_native_thread_and_call_components_cannot_collide_at_delimiters(native_runtime):
    case = await native_runtime(repository=True)
    attempt = await claim(case)
    workspace = await submission(case, attempt)
    runner = WorkerRunner(case.hub)
    await command(case.hub, "worker.bind_session", {"worker_id": attempt["worker_id"],
                                                   "thread_id": "thread:part"}, RUNNER)
    first = await call(runner, attempt, "thread:part", "stage", {"manifest_path": "submission.json"}, "call")
    await command(case.hub, "worker.bind_session", {"worker_id": attempt["worker_id"],
                                                   "thread_id": "thread"}, RUNNER)
    manifest = json.loads((workspace / "submission.json").read_text(encoding="utf-8"))
    manifest["data"]["summary"] = "A distinct native call."
    write(workspace, "submission.json", json.dumps(manifest))
    second = await call(runner, attempt, "thread", "stage", {"manifest_path": "submission.json"}, "part:call")
    assert first["artifact_id"] != second["artifact_id"]
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM native_call_receipts").fetchone()[0]) == 2


async def test_concurrent_native_retries_do_not_repeat_postcommit_validation(native_runtime, monkeypatch):
    case = await native_runtime(repository=True)
    attempt = await claim(case)
    await submission(case, attempt)
    runner = WorkerRunner(case.hub)
    calls = []
    original = runner._validate_submission

    async def validation(*args, **options):
        calls.append(args)
        return await original(*args, **options)

    monkeypatch.setattr(runner, "_validate_submission", validation)
    results = await asyncio.gather(*(call(runner, attempt, "native-thread", "stage",
        {"manifest_path": "submission.json"}, "same-call") for _ in range(2)))
    assert results[0] == results[1] and len(calls) == 1


async def test_invalid_saved_decision_is_committed_once_without_inference_replay(native_runtime):
    case = await native_runtime()
    await claim(case)
    await open_collaboration(case.hub, case.host["id"])
    independent = await command(case.hub, "worker.register")
    intent = await assignment_decision(case.hub, case.run["id"])
    assert intent["source_task_id"] == case.host["id"]
    # An answer in the retired selection shape is outside the decision schema. It counts
    # as no suitable idle worker, so the collaborator opening gets a new worker.
    harness = ScriptedHarness([ScriptedTurn([], visible_output=canonical({
        "task_id": case.host["id"], "expected_revision": 1, "plan": ""}))])
    runner = WorkerRunner(case.hub, lambda: harness)
    result = await runner.run_helper(intent)
    assert result["outcome"] == "new_worker" and result["reason"] == "none_suitable"
    assert result["assignment"]["task_id"] == case.host["id"]
    assert result["assignment"]["worker_id"] != independent["id"]
    for _ in range(3):
        assert not any(row["id"] == intent["id"] for row in await case.hub.due_assignment_intents(case.run["id"]))
        with pytest.raises(HubError) as replayed:
            await runner.run_helper(intent)  # Deliberately retained prepared object.
        assert replayed.value.code == "HELPER_RECONCILIATION_REQUIRED"
    current = await case.hub.db.call(lambda c: case.hub._helper(c, intent["id"]))
    assert current["state"] == "completed" and json.loads(current["raw_result_json"])["invalid_output"] is True
    assert not current["inference_uncertain"] and len(harness.prompts) == 1
    assert (await case.hub.run(case.run["id"]))["state"] == "running"


async def test_assignment_claim_race_completes_and_decides_again_instead_of_holding(native_runtime):
    case = await native_runtime()
    source = await command(case.hub, "worker.register")
    intent = await assignment_decision(case.hub, case.run["id"])
    assert intent["source_task_id"] == case.host["id"]
    await claim(case)  # Another worker wins the advertised initial opportunity.
    harness = ScriptedHarness([ScriptedTurn([], visible_output=canonical({"worker": source["display_name"]}))])
    result = await WorkerRunner(case.hub, lambda: harness).run_helper(intent)
    assert result["assignment"] is None and result["outcome"] == "opportunity_changed"
    current = await case.hub.db.call(lambda c: case.hub._helper(c, intent["id"]))
    assert current["state"] == "completed" and not current["inference_uncertain"]
    assert (await case.hub.worker(source["id"]))["work_intention"] == "idle"
    another = await command(case.hub, "task.publish", {"contract": {"title": "Another task", "objective": "New work."}})
    refreshed = await assignment_decision(case.hub, case.run["id"])
    assert refreshed["id"] != intent["id"] and refreshed["state"] == "prepared"
    assert refreshed["source_task_id"] == another["id"]
    assert [item["worker_id"] for item in refreshed["candidates"]["workers"]] == [source["id"]]


@pytest.mark.parametrize("kind", ["assignment", "recruitment"])
@pytest.mark.parametrize("inference_uncertain", [False, True])
async def test_monitor_exposes_helper_identity_usage_and_recovery_hold(native_runtime, kind, inference_uncertain):
    case = await native_runtime()
    if kind == "assignment":
        await command(case.hub, "worker.register")
        helper = await assignment_decision(case.hub, case.run["id"])
    else:
        attempt = await claim(case)
        await case.hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
        helper = (await case.hub.due_recruitment_intents(case.run["id"]))[0]
    await command(case.hub, "helper.started", {"intent_id": helper["id"], "thread_id": "helper-thread"}, RUNNER)
    await command(case.hub, "helper.turn.bind", {"intent_id": helper["id"], "remote_turn_id": "helper-turn"}, RUNNER)
    await command(case.hub, "helper.usage", {"intent_id": helper["id"], "sample_id": "usage-1", "delta_tokens": 25,
        "sample": {"totalTokens": 25, "lastTokenUsage": {"totalTokens": 25}}}, RUNNER)
    reader = SnapshotReader(case.root, run_id=case.run["id"])
    active = reader.collect()["database"]
    assert active["helpers"][0]["state"] == "running"
    assert active["helpers"][0]["thread_id"] == "helper-thread"
    assert active["helpers"][0]["remote_turn_id"] == "helper-turn"
    assert active["helpers"][0]["usage"]["tokens_used"] == active["run"]["tokens_used"] == 25
    assert not active["attempts"] and not active["turns"]
    await case.hub.helper_uncertain(helper["id"], "Synthetic recovery evidence.", inference_uncertain=inference_uncertain)
    records = Watchdog().observe(reader.collect())
    status = next(row for row in records if row["type"] == "snapshot")
    incident = next(row for row in records if row.get("code") == "helper_recovery_required")
    assert status["phase"] == ("running_with_issues" if inference_uncertain else "needs_attention")
    assert status["helpers"][0]["state"] == "uncertain"
    assert incident["subject_id"] == helper["id"]
    assert bool(incident["details"]["inference_uncertain"]) is inference_uncertain
    assert status["run"]["state"] == "running"
    assert bool(status["run"]["usage_uncertain"]) is inference_uncertain


async def test_helper_activity_and_diagnostics_are_separate_from_source_worker(native_runtime):
    case = await native_runtime()
    idle = await command(case.hub, "worker.register")
    helper = await assignment_decision(case.hub, case.run["id"])
    # An assignment decision's source is the Dispatcher, never the idle worker it may choose.
    assert helper["source_worker_id"] != idle["id"]
    assert (await case.hub.worker(helper["source_worker_id"]))["display_name"] == "Dispatcher"

    class DiagnosticHarness(ScriptedHarness):
        def set_activity_handler(self, handler):
            self.activity = handler

        def set_diagnostic_handler(self, handler):
            self.diagnostic = handler

        async def run_turn(self, prompt, **kwargs):
            await self.activity("model_activity", {"method": "synthetic"})
            await self.diagnostic({"message": "Synthetic local diagnostic.", "code": "synthetic_error"})
            return await super().run_turn(prompt, **kwargs)

    harness = DiagnosticHarness([ScriptedTurn([], usage={"totalTokens": 25}, visible_output=canonical({
        "worker": None}))])
    result = await WorkerRunner(case.hub, lambda: harness).run_helper(helper)
    assert result["directive"] == "assigned"
    telemetry = SnapshotReader(case.root, run_id=case.run["id"]).collect()["runtime_telemetry"]
    assert not telemetry["workers"]
    logged = telemetry["helpers"][0]
    assert logged["helper_id"] == helper["id"] and logged["source_worker_id"] == helper["source_worker_id"]
    assert logged["thread_id"] and logged["turn_id"] and logged["last_runtime_error"]["diagnostic_sha256"]
    assert logged["process_quiet_reported"]
