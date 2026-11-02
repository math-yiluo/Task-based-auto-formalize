"""Synthetic private-talk tests; no models, processes, or external services."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.faults import FaultIsolation
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.lifecycle_schema import (
    LIFECYCLE_SCHEMA,
)
from formal_geyser.coordination.models import Actor, Role
from formal_geyser.coordination.schema import SCHEMA
from formal_geyser.coordination.storage import Database, canonical
from formal_geyser.coordination.talk import (
    MAX_TALK_TEXT_BYTES,
    PEER_TALK_SCHEMA,
    PeerTalk,
)

OPERATOR = Actor(role=Role.OPERATOR)


def actor(worker="alice", *, fence=1):
    return Actor(role=Role.WORKER, worker_id=worker,
                 attempt_id="attempt-" + worker, fencing_token=fence)


class TalkHarness(FaultIsolation, PeerTalk):
    """Use real lease authorization with only locally seeded coordination state."""
    _get = Hub._get
    _role = Hub._role
    _active = Hub._active
    _effective_run_state = Hub._effective_run_state
    _worker_control = Hub._worker_control
    _control_of = Hub._control_of

    def _execution_environment(self, _connection):
        # This isolated private-message ledger has no compiler or native runtime.
        return None

    def __init__(self, root: Path):
        self.state_dir = root / ".task-based"
        self.now = 1000
        self.db = Database(self.state_dir / "state.sqlite3", SCHEMA + PEER_TALK_SCHEMA + LIFECYCLE_SCHEMA)

    def clock(self):
        # Every observation is a later instant.
        self.now += 1
        return self.now

    def _task(self, c, task_id):
        task = self._get(c, "tasks", task_id)
        return {**task, "revision": task["current_revision"]}

    async def send(self, sender="alice", recipient="bob", text="Private idea", message_id="one"):
        result = await self.db.call(lambda c: self._peer_talk_command(c, "talk.send", {
            "recipient_id": recipient, "message_id": message_id, "text": text}, actor(sender)),
            write=True)
        await self.reconcile_peer_talk()
        return result

    async def ack(self, delivery, worker="bob"):
        return await self.db.call(lambda c: self._peer_talk_command(
            c, "talk.ack", {"delivery_id": delivery}, actor(worker)), write=True)


@pytest.fixture
async def talk(tmp_path):
    harness = TalkHarness(tmp_path)

    def seed(c):
        c.execute("INSERT INTO runs(id,state,limits_json,created_at) VALUES('run','running','{}',0)")
        for task in ("home", "other", "review"):
            c.execute("INSERT INTO tasks(id,current_revision,state,created_at) VALUES(?,1,'claimed',0)",
                      (task,))
        c.execute("INSERT INTO task_collaboration(task_id,mode,host_task_id,governance,created_by,"
                  "created_at) VALUES('review','collaborative','home','protocol','alice',0)")
        for worker in ("alice", "bob", "carol", "outsider", "observer"):
            team = "other" if worker == "outsider" else "home"
            attempt = "attempt-" + worker
            c.execute("INSERT INTO workers(id,state,active_attempt,created_at) VALUES(?, 'running',?,0)",
                      (worker, attempt))
            c.execute("INSERT INTO task_members(task_id,worker_id,executor,joined_at) VALUES(?,?,?,0)",
                      (team, worker, int(worker != "observer")))
            c.execute("INSERT INTO attempts(id,task_id,task_revision,worker_id,run_id,fencing_token,"
                      "state,created_at) VALUES(?,?,1,?,'run',1,'running',0)",
                      (attempt, team, worker))
    await harness.db.call(seed, write=True)
    yield harness
    await harness.db.close()


async def test_files_are_authoritative_and_talk_has_no_task_side_effects(talk):
    tables = ("artifacts", "events", "notifications", "search_index", "review_requests",
              "continuations", "turn_intents", "task_members", "tasks", "workers", "attempts")

    async def snapshot():
        return await talk.db.call(lambda c: {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)] for table in tables})
    before = await snapshot()
    sent = await talk.send(text="An exact private idea: 位 \nsecond line")
    assert await snapshot() == before
    path = talk._peer_file(sent["conversation_id"], sent["id"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["text"] == "An exact private idea: 位 \nsecond line"
    assert saved["sender_id"] == "alice" and saved["recipient_id"] == "bob"
    assert "private" in path.parts and "peer-talk" in path.parts
    assert not (talk.state_dir / "objects").exists()
    assert await talk.db.call(lambda c: c.execute("SELECT count(*) FROM peer_append_intents").fetchone()[0]) == 0
    columns = await talk.db.call(lambda c: [row["name"] for row in c.execute("PRAGMA table_info(peer_messages)")])
    assert "text" not in columns and "content_json" not in columns


async def test_stable_message_ids_reject_changes_and_survive_reopen(talk):
    first = await talk.send()
    assert await talk.send() == first
    await talk.db.close()
    reopened = TalkHarness(talk.state_dir.parent)
    try:
        assert await reopened.send() == first
        with pytest.raises(HubError, match="different data"):
            await reopened.send(text="Changed")
        with pytest.raises(HubError, match="different data"):
            await reopened.send(recipient="carol")
        reply = await reopened.send("bob", "alice", "Reply", message_id="one")
        assert reply["id"] != first["id"]
        assert reply["conversation_id"] == first["conversation_id"]
        assert len(list((talk.state_dir / "private").rglob("*.json"))) == 2
    finally:
        await reopened.db.close()


async def test_rollback_never_writes_a_private_file(talk):
    def abort(c):
        talk._peer_talk_command(c, "talk.send", {
            "recipient_id": "bob", "message_id": "abort", "text": "Must not persist"}, actor())
        with pytest.raises(RuntimeError, match="transaction commit"):
            talk._reconcile_peer_talk(c)
        raise RuntimeError("Injected rollback")
    with pytest.raises(RuntimeError, match="Injected rollback"):
        await talk.db.call(abort, write=True)
    assert await talk.reconcile_peer_talk() == 0
    assert not (talk.state_dir / "private").exists()
    assert await talk.db.call(lambda c: c.execute("SELECT count(*) FROM peer_messages").fetchone()[0]) == 0


async def test_committed_append_recovers_before_and_after_file_write(talk, monkeypatch):
    sent = await talk.db.call(lambda c: talk._peer_talk_command(c, "talk.send", {
        "recipient_id": "bob", "message_id": "crash", "text": "Recover this exact text"}, actor()),
        write=True)
    write = talk._write_peer_intent

    def crash_after_write(row):
        write(row)
        raise OSError("Injected crash after file replace")
    monkeypatch.setattr(talk, "_write_peer_intent", crash_after_write)
    with pytest.raises(OSError, match="Injected crash"):
        await talk.reconcile_peer_talk()
    path = talk._peer_file(sent["conversation_id"], sent["id"])
    original = path.read_bytes()
    await talk.db.close()
    reopened = TalkHarness(talk.state_dir.parent)
    try:
        assert await reopened.reconcile_peer_talk() == 1
        assert path.read_bytes() == original
        assert await reopened.reconcile_peer_talk() == 0
        page = await reopened.peer_unread(actor("bob"))
        assert page["messages"][0]["text"] == "Recover this exact text"
    finally:
        await reopened.db.close()


@pytest.mark.parametrize("principal", [actor("bob"), Actor(role=Role.RUNNER), OPERATOR])
@pytest.mark.parametrize("reference_only", [False, True])
async def test_unread_handles_send_committed_after_its_reconciliation_job(
    talk, monkeypatch, principal, reference_only,
):
    original_call, original_reconcile = talk.db.call, talk._reconcile_peer_talk
    reconciled, sent = False, None
    text = "The concurrently committed peer message must remain exact. 位"

    def reconcile(c):
        nonlocal reconciled
        result = original_reconcile(c)
        reconciled = True
        return result

    async def call(function, write=False, **options):
        nonlocal sent
        result = await original_call(function, write=write, **options)
        if reconciled and sent is None:
            # Force the valid queue ordering: read/reconcile completes, then a
            # sender commits before the reader can submit its next DB job.
            sent = await original_call(lambda c: talk._peer_talk_command(c, "talk.send", {
                "recipient_id": "bob", "message_id": "between-jobs", "text": text}, actor()), write=True)
            assert not Path(sent["native_message"]["path"]).exists()
        return result

    monkeypatch.setattr(talk, "_reconcile_peer_talk", reconcile)
    monkeypatch.setattr(talk.db, "call", call)
    selected = {} if principal.role == Role.WORKER else {"worker_id": "bob"}
    page = await talk.peer_unread(principal, reference_only=reference_only, **selected)
    assert sent is not None
    if not page["messages"]:
        # A serialized read may finish before the concurrent send is committed.
        page = await talk.peer_unread(principal, reference_only=reference_only, **selected)
    assert [message["id"] for message in page["messages"]] == [sent["id"]]
    message = page["messages"][0]
    body = json.loads(Path(message["native_message"]["path"]).read_text(encoding="utf-8"))
    assert body["text"] == text and message["next_offset"] == len(text)
    assert ("text" not in message) if reference_only else message["text"] == text
    state = await talk.db.call(lambda c: dict(c.execute(
        "SELECT * FROM peer_messages WHERE id=?", (sent["id"],)).fetchone()))
    assert state["acknowledged_at"] is None and state["acknowledged_offset"] == 0
    assert (page["delivery_id"] is None) is (principal.role == Role.OPERATOR)
    if page["delivery_id"]:
        await talk.ack(page["delivery_id"])
        assert not (await talk.peer_unread(actor("bob")))["messages"]


@pytest.mark.parametrize("read_kind", ["history", "unread", "reference"])
async def test_peer_file_reads_handle_another_connection_committing_after_reconciliation(
    talk, monkeypatch, read_kind,
):
    first = await talk.send(text="Original committed message.")
    original_reconcile = talk._reconcile_peer_talk
    sent = None
    text = "Committed by another connection while the first reader prepares. 位"

    def reconcile(c):
        nonlocal sent
        result = original_reconcile(c)
        if sent is None:
            # A different SQLite connection can commit even while this
            # coordinator's single database callback is still running.
            other = sqlite3.connect(talk.db.path, autocommit=True)
            other.row_factory = sqlite3.Row
            try:
                other.execute("PRAGMA foreign_keys=ON")
                other.execute("BEGIN IMMEDIATE")
                sent = talk._peer_talk_command(other, "talk.send", {
                    "recipient_id": "bob", "message_id": "other-connection", "text": text}, actor())
                other.execute("COMMIT")
            finally:
                other.close()
            assert not Path(sent["native_message"]["path"]).exists()
        return result

    async def read():
        if read_kind == "history":
            return await talk.peer_history(first["conversation_id"], actor("bob"))
        return await talk.peer_unread(actor("bob"), reference_only=read_kind == "reference")

    monkeypatch.setattr(talk, "_reconcile_peer_talk", reconcile)
    initial = await read()
    assert initial["messages"][0]["id"] == first["id"]
    page = await read()
    assert [message["id"] for message in page["messages"]] == [first["id"], sent["id"]]
    assert [json.loads(Path(message["native_message"]["path"]).read_text(encoding="utf-8"))["text"]
            for message in page["messages"]] == ["Original committed message.", text]
    assert await talk.db.call(lambda c: c.execute(
        "SELECT count(*) FROM peer_messages WHERE acknowledged_at IS NOT NULL OR acknowledged_offset<>0").fetchone()[0]) == 0


@pytest.mark.parametrize("after_replace", [False, True])
async def test_unread_materialization_failure_retains_intent_without_delivery_or_ack(
    talk, monkeypatch, after_replace,
):
    sent = await talk.db.call(lambda c: talk._peer_talk_command(c, "talk.send", {
        "recipient_id": "bob", "message_id": "retry-write", "text": "Retain the exact committed message."}, actor()),
        write=True)
    write = talk._write_peer_intent

    def interrupted(row):
        if after_replace:
            write(row)
        raise OSError("Synthetic transcript materialization interruption")

    monkeypatch.setattr(talk, "_write_peer_intent", interrupted)
    with pytest.raises(OSError, match="materialization interruption"):
        await talk.peer_unread(actor("bob"), reference_only=True)
    assert not await talk.db.call(lambda c: c.in_transaction)
    assert await talk.db.call(lambda c: c.execute("SELECT count(*) FROM peer_append_intents").fetchone()[0]) == 1
    assert await talk.db.call(lambda c: c.execute("SELECT count(*) FROM peer_deliveries").fetchone()[0]) == 0
    monkeypatch.setattr(talk, "_write_peer_intent", write)
    page = await talk.peer_unread(actor("bob"), reference_only=True)
    assert [item["id"] for item in page["messages"]] == [sent["id"]]
    assert await talk.db.call(lambda c: c.execute(
        "SELECT acknowledged_offset,acknowledged_at FROM peer_messages").fetchone()[:]) == (0, None)
    assert len(list((Path(sent["native_message"]["path"]).parent).glob("*.json"))) == 1
    await talk.ack(page["delivery_id"])
    assert not (await talk.peer_unread(actor("bob")))["messages"]


async def test_failed_unread_receipt_rolls_back_without_acknowledging_message(talk):
    sent = await talk.send(text="Do not acknowledge a partially recorded delivery.")
    await talk.db.call(lambda c: c.execute(
        "CREATE TEMP TRIGGER fail_peer_item BEFORE INSERT ON peer_delivery_items "
        "BEGIN SELECT RAISE(ABORT, 'Synthetic delivery item failure'); END"))
    with pytest.raises(sqlite3.IntegrityError, match="delivery item failure"):
        await talk.peer_unread(actor("bob"))
    assert not await talk.db.call(lambda c: c.in_transaction)
    assert await talk.db.call(lambda c: c.execute("SELECT count(*) FROM peer_deliveries").fetchone()[0]) == 0
    assert await talk.db.call(lambda c: c.execute("SELECT count(*) FROM peer_delivery_items").fetchone()[0]) == 0
    assert await talk.db.call(lambda c: c.execute(
        "SELECT acknowledged_offset,acknowledged_at FROM peer_messages").fetchone()[:]) == (0, None)
    await talk.db.call(lambda c: c.execute("DROP TRIGGER fail_peer_item"))
    page = await talk.peer_unread(actor("bob"))
    assert [item["id"] for item in page["messages"]] == [sent["id"]]
    await talk.ack(page["delivery_id"])
    assert not (await talk.peer_unread(actor("bob")))["messages"]


@pytest.mark.parametrize("recipient", ["alice", "outsider", "observer", "missing"])
async def test_send_requires_a_distinct_existing_project_identity(talk, recipient):
    if recipient in {"alice", "missing"}:
        with pytest.raises(HubError):
            await talk.send(recipient=recipient)
        assert not (talk.state_dir / "private").exists()
    else:
        assert (await talk.send(recipient=recipient))["recipient_id"] == recipient


async def test_review_execution_can_read_and_send_without_changing_verdict(talk):
    first = await talk.send()
    await talk.db.call(lambda c: c.execute("UPDATE attempts SET task_id='review' WHERE worker_id='bob'"), write=True)
    await talk.send("bob", "alice", "The review has useful context")
    page = await talk.peer_history(first["conversation_id"], actor("bob"))
    assert len(page["messages"]) == 2


async def test_each_worker_pair_shares_one_conversation_without_task_binding(talk):
    first = await talk.send()
    assert (await talk.send("bob", "alice", "Reply", message_id="reply"))["conversation_id"] == first["conversation_id"]
    for context in ({"task_id": "home"}, {"artifact_id": "candidate"}):
        with pytest.raises(HubError, match="Unexpected"):
            await talk.db.call(lambda c: talk._peer_talk_command(c, "talk.send", {
                "recipient_id": "bob", "message_id": "scoped", "text": "Scoped", **context}, actor()), write=True)
    await talk.db.call(lambda c: c.execute("UPDATE tasks SET state='done'"), write=True)
    assert (await talk.send(message_id="after-done"))["conversation_id"] == first["conversation_id"]
    assert (await talk.send(recipient="outsider", message_id="outside"))["conversation_id"] != first["conversation_id"]
    columns = await talk.db.call(lambda c: [row["name"] for row in c.execute("PRAGMA table_info(peer_conversations)")])
    assert columns == ["id", "worker_low", "worker_high", "created_at"]
    saved = json.loads(talk._peer_file(first["conversation_id"], first["id"]).read_text(encoding="utf-8"))
    assert "team_id" not in saved and "task_id" not in saved
    assert len((await talk.peer_history(first["conversation_id"], OPERATOR))["messages"]) == 3


async def test_workers_send_consecutive_messages_without_a_rate_limit(talk):
    talk.clock = lambda: talk.now
    first = await talk.send()
    assert (await talk.send(recipient="carol", message_id="two"))["recipient_id"] == "carol"
    assert (await talk.send(message_id="three"))["conversation_id"] == first["conversation_id"]
    assert await talk.send() == first  # A replayed message ID is not a new send.
    assert await talk.db.call(lambda c: c.execute(
        "SELECT count(*) FROM peer_messages WHERE sender_id='alice'").fetchone()[0]) == 3


async def test_voluntary_exit_ends_send_rights_without_erasing_private_history(talk):
    first = await talk.send()
    await talk.db.call(lambda c: c.execute("UPDATE task_members SET membership_state='left' "
                                         "WHERE task_id='home' AND worker_id='bob'"), write=True)
    assert (await talk.send(message_id="after-exit"))["conversation_id"] == first["conversation_id"]
    assert (await talk.peer_history(first["conversation_id"], OPERATOR))["messages"][0]["text"] == "Private idea"


@pytest.mark.parametrize("principal", [OPERATOR, Actor(role=Role.RUNNER), actor(fence=2),
                                      Actor(role=Role.WORKER, worker_id="alice")])
async def test_send_requires_exact_fenced_worker_identity(talk, principal):
    with pytest.raises(HubError):
        await talk.db.call(lambda c: talk._peer_talk_command(c, "talk.send", {
            "recipient_id": "bob", "message_id": "bad-actor", "text": "Spoofed"}, principal),
            write=True)


async def test_payload_cannot_spoof_sender_or_task(talk):
    for extra in ({"sender_id": "bob"},):
        with pytest.raises(HubError, match="Unexpected"):
            await talk.db.call(lambda c: talk._peer_talk_command(c, "talk.send", {
                "recipient_id": "bob", "message_id": "spoof", "text": "Spoofed", **extra}, actor()),
                write=True)
    with pytest.raises(HubError):
        await talk.db.call(lambda c: talk._peer_talk_command(c, "talk.send", {
            "recipient_id": "bob", "message_id": "spoof", "text": "Spoofed", "task_id": "missing"}, actor()),
            write=True)
    with pytest.raises(HubError, match="16384"):
        await talk.send(text="位" * MAX_TALK_TEXT_BYTES)


async def test_project_history_reads_do_not_acknowledge_another_recipient(talk):
    sent = await talk.send()
    for principal in (actor("carol"), actor("outsider")):
        assert (await talk.peer_history(sent["conversation_id"], principal))["messages"][0]["text"] == "Private idea"
    with pytest.raises(HubError):
        await talk.peer_history(sent["conversation_id"], Actor(role=Role.RUNNER))
    observed = await talk.peer_unread(actor("carol"), worker_id="bob")
    assert observed["delivery_id"] is None
    own = await talk.peer_unread(actor("bob"))
    with pytest.raises(HubError):
        await talk.db.call(lambda c: talk._peer_ack(c, own["delivery_id"], "carol"), write=True)
    assert (await talk.peer_conversations(actor("carol"), worker_id="bob"))["conversations"]
    assert (await talk.peer_conversations(actor("carol")))["conversations"] == []
    operator = await talk.peer_unread(OPERATOR, worker_id="bob")
    assert operator["delivery_id"] is None and len(operator["messages"]) == 1


async def test_unread_retries_until_explicit_recipient_ack_and_stale_batches_are_safe(talk):
    sent = await talk.send()
    first = await talk.peer_unread(actor("bob"))
    retry = await talk.peer_unread(actor("bob"))
    assert first["messages"] == retry["messages"]
    assert first["delivery_id"] != retry["delivery_id"]
    for worker in ("alice", "carol"):
        with pytest.raises(HubError, match="Only the recipient"):
            await talk.ack(first["delivery_id"], worker)
    with pytest.raises(HubError, match="Only the recipient"):
        await talk.ack("unknown")
    await talk.ack(first["delivery_id"])
    assert await talk.ack(first["delivery_id"]) == {
        "delivery_id": first["delivery_id"], "acknowledged": True}
    await talk.ack(retry["delivery_id"])
    assert (await talk.peer_unread(actor("bob")))["messages"] == []
    assert (await talk.peer_history(sent["conversation_id"], actor("bob")))["messages"]


async def test_chunked_delivery_and_history_preserve_exact_unicode_under_budget(talk):
    text = "伪尾馃檪\n\"quoted\"" * 60
    sent = await talk.send(text=text)
    pieces = []
    for _ in range(100):
        page = await talk.peer_unread(actor("bob"), max_bytes=1024)
        assert len(canonical(page).encode("utf-8")) <= 1024
        if not page["messages"]:
            break
        assert len(page["messages"]) == 1
        item = page["messages"][0]
        assert item["offset"] == len("".join(pieces))
        pieces.append(item["text"])
        await talk.ack(page["delivery_id"])
    else:
        pytest.fail("Bounded delivery did not finish")
    assert "".join(pieces) == text
    pieces, cursor, offset = [], 0, 0
    for _ in range(100):
        page = await talk.peer_history(sent["conversation_id"], OPERATOR,
                                       after_sequence=cursor, offset=offset, max_bytes=1024)
        assert len(canonical(page).encode("utf-8")) <= 1024
        pieces.extend(item["text"] for item in page["messages"])
        cursor, offset = page["next_after_sequence"], page["next_offset"]
        if not page["has_more"]:
            break
    else:
        pytest.fail("Bounded history did not finish")
    assert "".join(pieces) == text


async def test_delivery_ack_cannot_consume_later_messages(talk):
    first = await talk.send()
    delivery = await talk.peer_unread(actor("bob"))
    second = await talk.send(text="Later message", message_id="two")
    await talk.ack(delivery["delivery_id"])
    unread = await talk.peer_unread(actor("bob"))
    assert [item["id"] for item in unread["messages"]] == [second["id"]]
    history = await talk.peer_history(first["conversation_id"], actor(), limit=1)
    assert history["has_more"] and history["messages"][0]["id"] == first["id"]
    history = await talk.peer_history(first["conversation_id"], actor(),
                                      after_sequence=history["next_after_sequence"], limit=1)
    assert not history["has_more"] and history["messages"][0]["id"] == second["id"]


async def test_corrupt_or_missing_authoritative_file_fails_closed(talk):
    sent = await talk.send()
    path = talk._peer_file(sent["conversation_id"], sent["id"])
    original = path.read_bytes()
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(HubError, match="integrity"):
        await talk.peer_unread(actor("bob"))
    path.write_bytes(original)
    path.unlink()
    with pytest.raises(HubError, match="missing"):
        await talk.peer_history(sent["conversation_id"], OPERATOR)
    assert await talk.db.call(lambda c: c.execute("SELECT count(*) FROM peer_deliveries").fetchone()[0]) == 0


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": True}, {"max_bytes": 511},
                                   {"max_bytes": 65537}, {"max_bytes": True}])
async def test_unread_rejects_unbounded_or_invalid_page_controls(talk, kwargs):
    with pytest.raises(HubError, match="Talk page"):
        await talk.peer_unread(actor("bob"), **kwargs)


@pytest.mark.parametrize("cursor", ["x" * 10000, "../escape", 3])
async def test_conversation_cursors_cannot_create_unbounded_responses(talk, cursor):
    with pytest.raises(HubError, match="cursor"):
        await talk.peer_conversations(actor(), after_id=cursor)


async def test_talk_does_not_wake_an_idle_recipient(talk):
    def idle(c):
        c.execute("UPDATE workers SET state='idle',active_attempt=NULL WHERE id='bob'")
        c.execute("UPDATE attempts SET state='released',ended_at=1000 WHERE worker_id='bob'")
        c.execute("UPDATE task_members SET runnable=0,work_reason=NULL WHERE worker_id='bob'")
    await talk.db.call(idle, write=True)
    await talk.send()
    with pytest.raises(HubError):
        await talk.peer_unread(actor("bob"))
    assert (await talk.peer_unread(OPERATOR, worker_id="bob"))["messages"]
    state = await talk.db.call(lambda c: dict(c.execute("SELECT * FROM workers WHERE id='bob'").fetchone()))
    assert state["state"] == "idle" and state["active_attempt"] is None


async def test_linked_private_directories_are_rejected(talk, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (talk.state_dir / "private").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("The host does not allow directory symlinks")
    with pytest.raises(HubError, match="linked paths"):
        await talk.send()
    assert list(elsewhere.iterdir()) == []


async def test_deep_runtime_transcripts_send_recover_read_and_ack(talk, tmp_path, monkeypatch):
    # Keep SQLite's existing runtime path below MAX_PATH while transcript paths
    # (conversation + immutable message name) exercise the extended I/O boundary.
    root = tmp_path / ("r" * max(16, 198 - len(str(tmp_path)) - 1))
    database = root / ".task-based/state.sqlite3"
    assert len(str(database)) < 260
    await talk.db.backup(database)
    deep = TalkHarness(root)
    original_write = deep._write_peer_intent

    def interrupted(row):
        original_write(row)
        raise OSError("Synthetic interruption after long-path append")
    monkeypatch.setattr(deep, "_write_peer_intent", interrupted)
    text = "Exact deep-runtime private conversation 位\n"
    try:
        with pytest.raises(OSError, match="long-path append"):
            await deep.send(text=text, message_id="deep-retry")
        metadata = await deep.db.call(lambda c: dict(c.execute(
            "SELECT * FROM peer_messages WHERE client_message_id='deep-retry'").fetchone()))
        logical = deep._peer_file(metadata["conversation_id"], metadata["id"])
        assert len(str(logical)) > 260
        original = logical.read_bytes()
        assert json.loads(original)["text"] == text
        assert await deep.db.call(lambda c: c.execute("SELECT count(*) FROM peer_append_intents").fetchone()[0]) == 1
    finally:
        await deep.db.close()
    restored = TalkHarness(root)
    try:
        replay = await restored.send(text=text, message_id="deep-retry")
        assert replay["id"] == metadata["id"]
        assert await restored.send(text=text, message_id="deep-retry") == replay
        assert logical.read_bytes() == original
        assert len(list(logical.parent.glob("*.json"))) == 1
        assert await restored.reconcile_peer_talk() == 0
        history = await restored.peer_history(replay["conversation_id"], OPERATOR)
        assert [item["text"] for item in history["messages"]] == [text]
        unread = await restored.peer_unread(actor("bob"))
        assert [item["text"] for item in unread["messages"]] == [text]
        await restored.ack(unread["delivery_id"])
        assert (await restored.peer_unread(actor("bob")))["messages"] == []
    finally:
        await restored.db.close()
