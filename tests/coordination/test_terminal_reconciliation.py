from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role

OP = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)


async def command(hub, name, payload, actor=OP, key=None):
    return await hub.execute(Command(name=name, key=key or uuid4().hex, payload=payload), actor)


@pytest.fixture
async def hub(tmp_path):
    initialize(tmp_path, ProjectConfig())
    instance = Hub(tmp_path)
    yield instance
    await instance.close()


async def uncertain_turn(hub, *, baseline=0):
    worker = await command(hub, "worker.register", {})
    worker = await command(hub, "worker.bind_session", {
        "worker_id": worker["id"], "thread_id": "thread-" + uuid4().hex,
        "model": "scripted", "codex_profile": hub.config.codex.model_dump(mode="json"),
    }, RUNNER)
    task = await command(hub, "task.publish", {"contract": {
        "title": "Interrupted software work", "objective": "Prepare a configuration record.",
    }})
    if baseline:
        old_run = await command(hub, "run.start", {})
        await command(hub, "task.claim", {
            "task_id": task["id"], "revision": 1, "worker_id": worker["id"],
            "run_id": old_run["id"],
        }, RUNNER)
        old_intent = await command(hub, "turn.reserve", {"worker_id": worker["id"]}, RUNNER)
        await command(hub, "turn.bind", {
            "intent_id": old_intent["id"], "remote_turn_id": "old-turn",
        }, RUNNER)
        await command(hub, "usage.record", {
            "worker_id": worker["id"], "sample": {"totalTokens": baseline},
        }, RUNNER)
        await command(hub, "turn.finish", {
            "intent_id": old_intent["id"], "status": "completed",
        }, RUNNER)
        await command(hub, "runtime.abandon", {
            "worker_id": worker["id"], "quiescent": True,
        }, RUNNER)
        await command(hub, "worker.recover", {"worker_id": worker["id"], "process_confirmed_stopped": True,
                      "reason": "The preceding completed synthetic native turn has stopped."})
        await command(hub, "run.pause", {"run_id": old_run["id"]})
    run = await command(hub, "run.start", {})
    attempt = await command(hub, "task.claim", {
        "task_id": task["id"], "revision": 1, "worker_id": worker["id"],
        "run_id": run["id"],
    }, RUNNER)
    intent = await command(hub, "turn.reserve", {"worker_id": worker["id"]}, RUNNER)
    remote = "turn-" + uuid4().hex
    await command(hub, "turn.bind", {
        "intent_id": intent["id"], "remote_turn_id": remote,
    }, RUNNER)
    await command(hub, "usage.record", {
        "worker_id": worker["id"], "sample": {"totalTokens": baseline + 40},
    }, RUNNER)
    await command(hub, "run.pause", {"run_id": run["id"]})
    await command(hub, "turn.finish", {
        "intent_id": intent["id"], "status": "unknown", "terminal_observed": False,
        "process_quiet": True,
    }, RUNNER)
    await command(hub, "runtime.abandon", {
        "worker_id": worker["id"], "quiescent": True,
    }, RUNNER)
    record = {
        "intent_id": intent["id"], "attempt_id": attempt["id"], "run_id": run["id"],
        "task_id": task["id"], "task_revision": 1, "worker_id": worker["id"],
        "thread_id": worker["thread_id"], "remote_turn_id": remote,
        "usage_epoch": worker["usage_epoch"], "status": "interrupted",
        "terminal_observed": True, "process_confirmed_stopped": True,
        "reason": "Reviewed the exact terminal, final usage, and owned process shutdown.",
        "cumulative_total_tokens": baseline + 40, "tokens_used": 40,
    }
    evidence = hub.root / "monitoring" / (intent["id"] + ".json")
    evidence.parent.mkdir(exist_ok=True)
    body = json.dumps({
        "native_terminal": "interrupted", "identity": record, "owned_processes_stopped": True,
    }).encode()
    evidence.write_bytes(body)
    record["evidence"] = {
        "path": evidence.relative_to(hub.root).as_posix(),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    return record


async def unchanged(hub, payload):
    intent = await hub.db.call(lambda c: hub._get(c, "turn_intents", payload["intent_id"]))
    assert intent["state"] == "closed_unknown"
    run = await hub.run(payload["run_id"])
    assert run["usage_uncertain"] == 1
    assert run["tokens_used"] == 40
    assert not await hub.list_interruptions()
    assert not any(e["kind"] == "turn.terminal_reconciled" for e in await hub.events())


async def import_reconciled(hub, payload):
    return await command(hub, "interruption.import", {
        **{key: payload[key] for key in (
            "attempt_id", "task_id", "task_revision", "worker_id", "thread_id",
            "remote_turn_id", "usage_epoch",
        )},
        "turn_intent_id": payload["intent_id"],
        "codex_profile": hub.config.codex.model_dump(mode="json"),
        "terminal_observed": True, "process_quiet": True, "process_confirmed_stopped": True,
        "reason": "Import the independently reconciled interrupted conversation.",
        "evidence": "Verified evidence SHA-256: " + payload["evidence"]["sha256"],
        "stop_reason": "operator_pause",
    })


async def test_verified_terminal_reconciliation_is_atomic_idempotent_and_separate_from_import(hub):
    payload = await uncertain_turn(hub)
    key = uuid4().hex
    result = await command(hub, "turn.reconcile_terminal", payload, key=key)
    assert result["turn_intent"]["state"] == "interrupted"
    assert result["run"]["state"] == "paused"
    assert result["run"]["usage_uncertain"] == 0
    assert result["run"]["tokens_used"] == 40
    assert result["native_usage_delta"] == result["added_tokens"] == 0
    assert (await hub.worker(payload["worker_id"]))["state"] == "idle"
    assert not await hub.list_interruptions()
    evidence_path = hub.root / payload["evidence"]["path"]
    saved = evidence_path.read_bytes()
    evidence_path.unlink()
    assert hub.blobs.get(result["evidence_sha256"]) == saved
    assert await command(hub, "turn.reconcile_terminal", payload, key=key) == result
    events = [e for e in await hub.events() if e["kind"] == "turn.terminal_reconciled"]
    assert len(events) == 1
    assert events[0]["payload"]["previous_state"] == "closed_unknown"
    assert events[0]["payload"]["process_confirmed_stopped"] is True
    with pytest.raises(HubError) as conflict:
        await command(hub, "turn.reconcile_terminal", {**payload, "tokens_used": 41}, key=key)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    await import_reconciled(hub, payload)
    new_run = await command(hub, "run.start", {})
    ticket = (await hub.list_interruptions())[0]
    resumed = await command(hub, "interruption.resume", {
        "interruption_id": ticket["id"], "run_id": new_run["id"],
    }, RUNNER)
    assert resumed["worker_id"] == payload["worker_id"]
    assert (await hub.worker(payload["worker_id"]))["thread_id"] == payload["thread_id"]


async def test_reconciliation_preserves_cross_run_cumulative_baseline_and_charges_missing_usage(hub):
    payload = await uncertain_turn(hub, baseline=200)
    payload.update(cumulative_total_tokens=250, tokens_used=53)
    result = await command(hub, "turn.reconcile_terminal", payload)
    assert result["native_usage_delta"] == 10
    assert result["added_tokens"] == 13
    assert result["run"]["tokens_used"] == 53
    await import_reconciled(hub, payload)
    next_run = await command(hub, "run.start", {})
    ticket = (await hub.list_interruptions())[0]
    await command(hub, "interruption.resume", {
        "interruption_id": ticket["id"], "run_id": next_run["id"],
    }, RUNNER)
    await command(hub, "usage.record", {
        "worker_id": payload["worker_id"], "sample": {"totalTokens": 257},
    }, RUNNER)
    assert (await hub.run(next_run["id"]))["tokens_used"] == 7
    assert (await hub.run(payload["run_id"]))["tokens_used"] == 53


@pytest.mark.parametrize("role", [Role.RUNNER, Role.WORKER, Role.VALIDATOR])
async def test_terminal_reconciliation_is_operator_only(hub, role):
    payload = await uncertain_turn(hub)
    with pytest.raises(HubError) as blocked:
        await command(hub, "turn.reconcile_terminal", payload, Actor(role=role))
    assert blocked.value.code == "FORBIDDEN"
    await unchanged(hub, payload)


@pytest.mark.parametrize("field", [
    "attempt_id", "run_id", "task_id", "task_revision", "worker_id",
    "thread_id", "remote_turn_id", "usage_epoch",
])
async def test_reconciliation_requires_exact_bound_identity(hub, field):
    payload = await uncertain_turn(hub)
    changed = {**payload, field: payload[field] + 1 if type(payload[field]) is int else "different"}
    with pytest.raises(HubError) as blocked:
        await command(hub, "turn.reconcile_terminal", changed)
    assert blocked.value.code == "SESSION_CHANGED"
    await unchanged(hub, payload)


@pytest.mark.parametrize("field,value,code", [
    ("terminal_observed", False, "QUIESCENCE_REQUIRED"),
    ("process_confirmed_stopped", False, "QUIESCENCE_REQUIRED"),
    ("process_confirmed_stopped", 1, "QUIESCENCE_REQUIRED"),
    ("reason", "", "EVIDENCE_REQUIRED"),
    ("reason", " " * 4, "EVIDENCE_REQUIRED"),
    ("status", "unknown", "TERMINAL_REQUIRED"),
    ("tokens_used", 39, "INVALID_USAGE"),
    ("tokens_used", True, "INVALID_USAGE"),
    ("cumulative_total_tokens", 39, "INVALID_USAGE"),
    ("cumulative_total_tokens", 50, "INVALID_USAGE"),
    ("evidence", {}, "EVIDENCE_REQUIRED"),
])
async def test_incomplete_evidence_and_usage_remain_quarantined(hub, field, value, code):
    payload = await uncertain_turn(hub)
    with pytest.raises(HubError) as blocked:
        await command(hub, "turn.reconcile_terminal", {**payload, field: value})
    assert blocked.value.code == code
    await unchanged(hub, payload)
    with pytest.raises(HubError) as cannot_import:
        await import_reconciled(hub, payload)
    assert cannot_import.value.code == "INTERRUPTED_TURN_REQUIRED"


@pytest.mark.parametrize("change,code", [
    ("missing", "EVIDENCE_REQUIRED"), ("mismatch", "EVIDENCE_MISMATCH"),
    ("outside", "EVIDENCE_REQUIRED"), ("oversize", "EVIDENCE_REQUIRED"),
])
async def test_evidence_reference_is_bounded_local_and_digest_checked(hub, change, code):
    payload = await uncertain_turn(hub)
    path = hub.root / payload["evidence"]["path"]
    if change == "missing":
        path.unlink()
    elif change == "mismatch":
        path.write_text("Changed after operator review")
    elif change == "outside":
        payload["evidence"]["path"] = str(path.resolve())
    else:
        body = b"x" * 1048577
        path.write_bytes(body)
        payload["evidence"]["sha256"] = hashlib.sha256(body).hexdigest()
    with pytest.raises(HubError) as blocked:
        await command(hub, "turn.reconcile_terminal", payload)
    assert blocked.value.code == code
    await unchanged(hub, payload)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_other_verified_terminals_do_not_become_interruption_tickets(hub, status):
    payload = await uncertain_turn(hub)
    payload["status"] = status
    result = await command(hub, "turn.reconcile_terminal", payload)
    assert result["turn_intent"]["state"] == status
    with pytest.raises(HubError) as blocked:
        await import_reconciled(hub, payload)
    assert blocked.value.code == "INTERRUPTED_TURN_REQUIRED"


async def test_reconciliation_does_not_clear_another_unresolved_intent(hub):
    payload = await uncertain_turn(hub)
    other_worker = await command(hub, "worker.register", {})
    await hub.db.call(lambda c: c.execute(
        "INSERT INTO turn_intents(id,worker_id,attempt_id,run_id,state,created_at) VALUES(?,?,?,?,?,?)",
        ("other", other_worker["id"], payload["attempt_id"], payload["run_id"], "uncertain", 1),
    ), write=True)
    result = await command(hub, "turn.reconcile_terminal", payload)
    assert result["run"]["usage_uncertain"] == 1
    assert await hub.db.call(lambda c: c.execute("SELECT state FROM turn_intents WHERE id='other'").fetchone()[0]) == "uncertain"


@pytest.mark.parametrize("change,code", [
    ("active_worker", "RECONCILIATION_NOT_SAFE"),
    ("latest_attempt", "SESSION_CHANGED"),
    ("latest_intent", "SESSION_CHANGED"),
])
async def test_reconciliation_rejects_resumed_or_newer_execution(hub, change, code):
    payload = await uncertain_turn(hub)

    def alter(c):
        if change == "active_run":
            c.execute("UPDATE runs SET state='running' WHERE id=?", (payload["run_id"],))
        elif change == "active_worker":
            c.execute("UPDATE workers SET state='running',active_attempt=? WHERE id=?",
                      (payload["attempt_id"], payload["worker_id"]))
        elif change == "latest_attempt":
            c.execute(
                "INSERT INTO attempts(id,task_id,task_revision,worker_id,run_id,fencing_token,"
                "state,created_at,ended_at) VALUES(?,?,?,?,?,?,?,9999999999999,9999999999999)",
                ("newer-attempt", payload["task_id"], 1, payload["worker_id"],
                 payload["run_id"], 2, "abandoned"),
            )
        else:
            c.execute(
                "INSERT INTO turn_intents(id,worker_id,attempt_id,run_id,state,created_at)"
                " VALUES(?,?,?,?,?,9999999999999)",
                ("newer-intent", payload["worker_id"], payload["attempt_id"],
                 payload["run_id"], "completed"),
            )

    await hub.db.call(alter, write=True)
    with pytest.raises(HubError) as blocked:
        await command(hub, "turn.reconcile_terminal", payload)
    assert blocked.value.code == code
    await unchanged(hub, payload)


async def test_usage_only_reconciliation_does_not_assert_a_known_terminal(hub):
    payload = await uncertain_turn(hub)
    await command(hub, "run.reconcile_usage", {
        "run_id": payload["run_id"], "tokens_used": 40,
        "reason": "Conservative accounting only; no terminal proof.",
    })
    with pytest.raises(HubError) as blocked:
        await import_reconciled(hub, payload)
    assert blocked.value.code == "INTERRUPTED_TURN_REQUIRED"


async def test_failed_reconciliation_rolls_back_terminal_and_usage_together(hub, monkeypatch):
    payload = await uncertain_turn(hub)
    payload.update(cumulative_total_tokens=50, tokens_used=50)
    original = hub._event

    def fail_record(c, kind, *args, **kwargs):
        if kind == "turn.terminal_reconciled":
            raise HubError("INJECTED_FAILURE", "The test interrupted the audit transaction")
        return original(c, kind, *args, **kwargs)

    monkeypatch.setattr(hub, "_event", fail_record)
    with pytest.raises(HubError) as blocked:
        await command(hub, "turn.reconcile_terminal", payload)
    assert blocked.value.code == "INJECTED_FAILURE"
    await unchanged(hub, payload)
    maximum = await hub.db.call(lambda c: c.execute(
        "SELECT MAX(total) FROM usage_samples WHERE worker_id=? AND epoch=?",
        (payload["worker_id"], payload["usage_epoch"]),
    ).fetchone()[0])
    assert maximum == 40


async def test_reconciled_terminal_survives_reopen_without_automatic_work(hub):
    payload = await uncertain_turn(hub)
    result = await command(hub, "turn.reconcile_terminal", payload)
    await hub.close()
    reopened = Hub(hub.root)
    try:
        run = await reopened.run(payload["run_id"])
        assert run["state"] == "paused"
        assert run["usage_uncertain"] == 0
        assert (await reopened.worker(payload["worker_id"]))["active_attempt"] is None
        assert not await reopened.list_interruptions()
        assert reopened.blobs.get(result["evidence_sha256"])
    finally:
        await reopened.close()
