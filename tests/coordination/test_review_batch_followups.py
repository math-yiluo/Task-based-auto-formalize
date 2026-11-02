"""Review regressions: cheap scheduling, explicit retries and narrow communication."""
import sqlite3

import pytest

from formal_geyser.coordination.batch_integration import BATCH_DELAY_MS, BATCH_THRESHOLD
from formal_geyser.coordination.errors import HubError
from tests.coordination.test_collaborative_core import command, quiet, worker_actor
from tests.coordination.test_integrator_batches import batch_case as batch_case
from tests.coordination.test_integrator_batches import choose, ready
from tests.coordination.test_peer_talk import talk as talk


async def test_admission_and_status_skip_expensive_checks_before_due(batch_case, monkeypatch):
    hub, run, _ = batch_case
    await ready(hub, run, "Not due yet")
    def unexpected(*args, **kwargs):
        raise AssertionError("No candidate file checks before the timer or threshold")
    monkeypatch.setattr(hub, "_batch_eligible", unexpected)
    assert await choose(hub, run) is None
    assert len((await hub.integration_queue())["reviews"]) == 1


async def test_enable_is_one_time_and_never_mutates_later_correspondence(batch_case):
    hub, run, _ = batch_case
    reviewer = await ready(hub, run, "Keep later state")
    await hub.db.call(lambda c: c.execute("UPDATE workers SET work_intention='working' WHERE id=?",
                                        (reviewer["worker_id"],)), write=True)
    assert await hub.db.call(hub._enable_integrator, write=True)
    assert (await hub.worker(reviewer["worker_id"]))["work_intention"] == "working"
    assert await hub.db.call(lambda c: c.execute(
        "SELECT count(*) FROM events WHERE kind='integration.enabled'").fetchone()[0]) == 1


async def test_operator_retry_needs_reason_and_preserves_batch_boundary(batch_case):
    hub, run, now = batch_case
    await ready(hub, run, "Retry only after explicit evidence")
    now[0] += BATCH_DELAY_MS
    attempt = await choose(hub, run)
    batch = await hub.db.call(lambda c: hub._batch(c))
    candidate = batch["items"][0]["candidate_id"]
    await command(hub, "integrator.defer", {"candidate_id": candidate, "reason": "Source clarification missing"}, worker_actor(attempt))
    queue = await hub.integration_queue()
    assert queue["threshold"] == BATCH_THRESHOLD and queue["delay_minutes"] == BATCH_DELAY_MS // 60000
    assert queue["reviews"][0]["deferred_at"] == now[0]
    assert queue["holder"]["review_task_ids"] == []
    with pytest.raises(HubError):
        await command(hub, "integrator.retry", {"candidate_id": candidate})
    result = await command(hub, "integrator.retry", {"candidate_id": candidate, "reason": "The source has been supplied."})
    assert result["queued_for_next_batch"]
    assert (await hub.integration_queue())["reviews"][0]["deferred_at"] is None
    assert (await hub.db.call(lambda c: hub._batch(c)))["items"][0]["state"] == "deferred"
    await command(hub, "integrator.finish_empty", {}, worker_actor(attempt))
    await quiet(hub, attempt)
    assert await choose(hub, run) is None
    now[0] += BATCH_DELAY_MS
    assert await choose(hub, run) is not None


async def test_batch_state_constraint_and_payload_errors_are_specific(batch_case):
    hub, run, now = batch_case
    await ready(hub, run, "Exact batch state")
    now[0] += BATCH_DELAY_MS
    attempt = await choose(hub, run)
    with pytest.raises(HubError) as missing:
        await command(hub, "integrator.defer", {}, worker_actor(attempt))
    assert missing.value.code == "INVALID_INTEGRATOR_INPUT"
    with pytest.raises(sqlite3.IntegrityError):
        await hub.db.call(lambda c: c.execute("UPDATE integration_batches SET state='typo'"), write=True)


async def test_ordinary_message_only_recipient_can_receive_project_contact(talk):
    await talk.db.call(lambda c: c.execute("UPDATE workers SET execution_mode='message_only' WHERE id='bob'"), write=True)
    sent = await talk.send("alice", "bob")
    assert sent["recipient_id"] == "bob"


async def test_next_batch_status_keeps_previous_live_deferrals_visible(batch_case):
    hub, run, now = batch_case
    await ready(hub, run, "Earlier deferred contribution")
    now[0] += BATCH_DELAY_MS
    first = await choose(hub, run)
    original = await command(hub, "integrator.status", {}, worker_actor(first))
    candidate = original["items"][0]["candidate_id"]
    await command(hub, "integrator.defer", {"candidate_id": candidate, "reason": "Source missing."}, worker_actor(first))
    await command(hub, "integrator.finish_empty", {}, worker_actor(first))
    await quiet(hub, first)
    await ready(hub, run, "Next contribution")
    now[0] += BATCH_DELAY_MS
    second = await choose(hub, run)
    status = await command(hub, "integrator.status", {}, worker_actor(second))
    assert candidate not in {item["candidate_id"] for item in status["items"]}
    assert status["deferred"][0]["candidate_id"] == candidate
    await command(hub, "artifact.withdraw", {"artifact_id": candidate, "reason": "The author abandoned this route."})
    status = await command(hub, "integrator.status", {}, worker_actor(second))
    assert not status["deferred"]
