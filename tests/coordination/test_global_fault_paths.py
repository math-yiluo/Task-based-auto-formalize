"""A local observation or bookkeeping fault never stops unrelated execution."""
from __future__ import annotations

import logging

from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime import runner as runner_module
from formal_geyser.coordination.runtime.telemetry import ActivityLog
from formal_geyser.coordination.transcripts import TranscriptStore
from tests.coordination.test_fault_isolation_policy import peers as peers


async def test_unwritable_activity_log_never_blocks_a_turn(peers):
    hub, run, (attempt, _) = peers
    harness = ScriptedHarness([ScriptedTurn([])])
    runner = WorkerRunner(hub, lambda: harness)
    runner.activity.path.unlink(missing_ok=True)
    runner.activity.path.mkdir(parents=True)  # Appending now fails, as with a locked or full file.
    assignment = await hub.db.call(lambda c: hub._assignment_metadata(c, attempt))
    await runner.run_once(run["id"], assignment=assignment)
    assert len(harness.prompts) == 1


async def test_activity_log_failures_are_reported_once_per_streak(tmp_path, caplog):
    log = ActivityLog(tmp_path)
    log.path.mkdir()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await log.emit("activation_started")
    assert sum("not writable" in record.getMessage() for record in caplog.records) == 1


async def test_one_missing_history_record_does_not_hold_back_the_others(peers):
    hub, _, _ = peers
    store = TranscriptStore(hub.root)
    kept, missing = store.prepare_input("worker-a", "thread-a", "runner", "Prompt A"), "0" * 32

    def seed(c):
        for delivery, input_id in (("kept", kept), ("missing", missing)):
            c.execute("INSERT INTO delivery_transcript_updates(kind,delivery_id,input_id,state,turn_id,reason) "
                      "VALUES('peer',?,?,'not_delivered',NULL,'Transport closed before delivery')", (delivery, input_id))
    await hub.db.call(seed, write=True)
    assert await hub.reconcile_delivery_transcripts() == 1
    applied = await hub.db.call(lambda c: {row[0]: row[1] for row in c.execute(
        "SELECT delivery_id,applied FROM delivery_transcript_updates")})
    # The unreadable record stays unapplied for a later retry; the others are done.
    assert applied == {"kept": 1, "missing": 0}
    assert hub.unapplied_transcript_updates == [{"input_id": missing, "error": "HubError"}]


async def test_unreadable_ownership_record_leaves_only_that_execution_held(peers):
    hub, run, (attempt, other) = peers
    await hub.note_owned_process("worker", attempt["id"], {"format": 2, "cgroup": "/owner/tree-" + "0" * 32})
    await hub.db.call(lambda c: c.execute(
        "UPDATE events SET payload_json='{not json' WHERE kind='execution.process_owned' AND object_id=?",
        (attempt["id"],)), write=True)
    await hub.close()
    reopened = Hub(hub.root)
    try:
        await reopened.recover_quiet_runtime_failures(run["id"])
        assert (await reopened.worker(attempt["worker_id"]))["active_attempt"] == attempt["id"]
        assert (await reopened.worker(other["worker_id"]))["active_attempt"] is None
    finally:
        await reopened.close()


async def test_recovery_failure_does_not_stop_new_work(peers, monkeypatch):
    hub, run, (_, healthy) = peers
    runner = WorkerRunner(hub, ScriptedHarness)
    selections, started = [healthy], []

    async def broken(*_args, **_kwargs):
        raise RuntimeError("Synthetic recovery defect")

    async def select(_run_id):
        return selections.pop() if selections else None

    async def activation(_run_id, *, assignment):
        started.append(assignment["worker_id"])
        return {"worker_id": assignment["worker_id"], "turn_status": "completed"}
    monkeypatch.setattr(hub, "recover_quiet_runtime_failures", broken)
    monkeypatch.setattr(runner, "_select", select)
    monkeypatch.setattr(runner, "run_once", activation)
    await runner.run_until_idle(run["id"])
    assert started == [healthy["worker_id"]]


async def test_a_long_admission_pass_still_lets_recovery_and_helpers_run(peers, monkeypatch):
    hub, run, _ = peers
    runner = WorkerRunner(hub, ScriptedHarness)
    order, waiting = [], [{"id": f"attempt-{number}", "worker_id": f"worker-{number}"} for number in (3, 2, 1)]

    async def recover(*_args, **_kwargs):
        order.append("recover")

    async def intents(_run_id):
        order.append("helpers")
        return []

    async def select(_run_id):
        order.append("admit" if waiting else "none")
        return waiting.pop() if waiting else None

    async def activation(_run_id, *, assignment):
        return {"worker_id": assignment["worker_id"], "turn_status": "completed"}
    monkeypatch.setattr(runner_module, "ADMISSION_PASS_SECONDS", 0)
    monkeypatch.setattr(hub, "recover_quiet_runtime_failures", recover)
    monkeypatch.setattr(hub, "due_recruitment_intents", intents)
    monkeypatch.setattr(hub, "due_assignment_intents", intents)
    monkeypatch.setattr(runner, "_select", select)
    monkeypatch.setattr(runner, "run_once", activation)
    await runner.run_until_idle(run["id"])
    # Each admission ends its pass, so recovery and the helpers run between admissions.
    passes = "|".join(order).split("recover|")[1:]
    assert passes[:3] == ["admit|helpers|helpers|"] * 3
