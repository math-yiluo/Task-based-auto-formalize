"""Request identity, full native references, and safe operator response retries."""
from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Role
from formal_geyser.coordination.storage import canonical, digest
from tests.coordination.communication_helpers import native_text
from tests.coordination.test_communications import OPERATOR, WORKER
from tests.coordination.test_communications import comm as comm
from tests.coordination.test_peer_talk import actor
from tests.coordination.test_peer_talk import talk as talk

BOB = Actor(role=Role.WORKER, worker_id="bob", attempt_id="attempt-bob", fencing_token=1)
QUESTION = {"request_key": "choose-convention", "title": "Convention A", "text": "Use a left action?"}


async def test_questions_are_task_and_revision_scoped_but_identical_teammates_join(comm):
    first = await comm.execute("message.request", QUESTION, WORKER)
    assert (first["task_id"], first["task_revision"]) == ("task", 1)
    assert (await comm.execute("message.request", QUESTION, BOB))["id"] == first["id"]
    def another_task(c):
        c.execute("INSERT INTO tasks(id,current_revision,state,created_at) VALUES('other',1,'claimed',0)")
        c.execute("UPDATE attempts SET task_id='other' WHERE worker_id='bob'")
    await comm.db.call(another_task, write=True)
    second = await comm.execute("message.request", {**QUESTION, "title": "Convention B",
                                "text": "Use cohomological indexing?"}, BOB)
    assert second["id"] != first["id"]
    await comm.execute("request.resolve", {"request_id": first["id"], "text": "Use a left action.", "message_id": "answer-a"})
    assert not await comm.db.call(lambda c: comm._external_wait_satisfied(c, second["wait_predicate"]))
    def revise(c):
        c.execute("UPDATE tasks SET current_revision=2 WHERE id='task'")
        c.execute("UPDATE attempts SET task_revision=2 WHERE worker_id='alice'")
    await comm.db.call(revise, write=True)
    revised = await comm.execute("message.request", QUESTION, WORKER)
    assert revised["id"] not in {first["id"], second["id"]}
    assert not await comm.db.call(lambda c: comm._external_wait_satisfied(c, revised["wait_predicate"]))


@pytest.mark.parametrize("command", ["message.request", "literature.request"])
@pytest.mark.parametrize("changed", [{"title": "Another title"}, {"text": "A different question?"}])
async def test_request_identity_conflicts_do_not_add_waiters_or_change_native_content(comm, command, changed):
    request = await comm.execute(command, QUESTION, WORKER)
    body = Path(request["path"]).read_bytes()
    with pytest.raises(HubError) as error:
        await comm.execute(command, {**QUESTION, **changed}, BOB)
    assert error.value.code == "REQUEST_IDENTITY_CONFLICT"
    assert Path(request["path"]).read_bytes() == body
    assert (await comm.list_human_requests())[0]["workers"] == ["alice"]
    assert (await comm.execute(command, QUESTION, WORKER))["id"] == request["id"]


async def test_unscoped_historical_question_is_preserved_without_joining_it(comm):
    key = digest({"kind": "question", "key": QUESTION["request_key"]})
    request_id = uuid5(NAMESPACE_URL, "external-request:" + key).hex
    relative = f"correspondence/requests/{request_id}.md"
    metadata = {"id": request_id, "kind": "question", "request_key": QUESTION["request_key"],
                "title": QUESTION["title"], "created_at": 5}
    sha = comm._write_document(relative, metadata, QUESTION["text"])
    def seed(c):
        c.execute("INSERT INTO external_requests VALUES(?,?,?,'fulfilled',?,?,NULL,5,6)",
                  (request_id, key, "question", relative, sha))
        c.execute("INSERT INTO external_request_waiters VALUES(?,?)", (request_id, "alice"))
        return dict(c.execute("SELECT * FROM external_requests WHERE id=?", (request_id,)).fetchone())
    original = await comm.db.call(seed, write=True)
    body = (comm.root / relative).read_bytes()
    new = await comm.execute("message.request", QUESTION, WORKER)
    assert new["id"] != request_id and new["state"] == "pending"
    assert await comm.db.call(lambda c: dict(c.execute("SELECT * FROM external_requests WHERE id=?", (request_id,)).fetchone())) == original
    assert (comm.root / relative).read_bytes() == body


async def test_native_human_reference_includes_the_end_and_preserves_transport_uncertainty(comm):
    text = "Execute the operation. " + "Background details. " * 800 + "Only after verifying the final condition."
    sent = await comm.execute("message.send", {"worker_id": "alice", "text": text, "message_id": "long"})
    page = await comm.human_unread(WORKER, max_bytes=4096, reference_only=True)
    message = page["messages"][0]
    assert "Read each native_message.path in full" in page["instruction"]
    assert page["delivery_mode"] == "native_reference" and not page["has_more"]
    assert "text" not in message and message["complete"]
    assert message["offset"] == 0 and message["next_offset"] == len(text)
    assert message["sender_role"] == "operator" and native_text(message) == text
    assert message["native_message"] == sent["native_message"]
    await comm.mark_delivery_uncertain(WORKER, "human", page["delivery_id"], "thread", "turn", "input", pending=True)
    assert not (await comm.human_unread(WORKER, reference_only=True))["messages"]
    await comm.mark_delivery_uncertain(WORKER, "human", page["delivery_id"], "thread", "turn", "input")
    assert (await comm.human_history("alice"))["messages"][0]["acknowledged_offset"] == 0
    await comm.execute("message.delivery.resolve", {"kind": "human", "delivery_id": page["delivery_id"],
                       "outcome": "delivered", "reason": "The exact native reference input was accepted."})
    assert not (await comm.human_unread(WORKER, reference_only=True))["messages"]
    history = (await comm.human_history("alice"))["messages"][0]
    assert history["acknowledged_offset"] == len(text) and native_text(history) == text


async def test_native_references_cover_worker_to_user_and_resume_partial_human_delivery(comm):
    outgoing = await comm.execute("message.send", {"text": "Complete worker report.", "message_id": "report"}, WORKER)
    assert native_text(outgoing) == "Complete worker report." and outgoing["sender_role"] == "worker"
    text = "Exact human guidance λ. " * 300
    await comm.execute("message.send", {"worker_id": "alice", "text": text, "message_id": "human"})
    chunk = await comm.human_unread(WORKER, max_bytes=1024)
    assert not chunk["messages"][0]["complete"]
    assert native_text(chunk["messages"][0]) == text
    await comm.execute("message.ack", {"delivery_id": chunk["delivery_id"]}, WORKER)
    reference = await comm.human_unread(WORKER, max_bytes=4096, reference_only=True)
    assert reference["messages"][0]["offset"] == chunk["messages"][0]["next_offset"]
    assert reference["messages"][0]["next_offset"] == len(text)
    await comm.execute("message.ack", {"delivery_id": reference["delivery_id"]}, WORKER)
    assert not (await comm.human_unread(WORKER))["messages"]


async def test_peer_reference_is_exact_for_both_directions_and_completes_once(talk):
    text = "Peer suggestion. " * 600 + "Check the final qualification first."
    sent = await talk.send(text=text)
    assert native_text(sent) == text
    first = await talk.peer_unread(actor("bob"), max_bytes=1024)
    assert not first["messages"][0]["complete"]
    await talk.ack(first["delivery_id"])
    page = await talk.peer_unread(actor("bob"), max_bytes=4096, reference_only=True)
    message = page["messages"][0]
    assert len(canonical(page).encode("utf-8")) <= 4096
    assert "text" not in message and message["sender_role"] == "worker"
    assert message["offset"] == first["messages"][0]["next_offset"]
    assert message["next_offset"] == len(text) and native_text(message) == text
    assert message["native_message"] == sent["native_message"]
    await talk.ack(page["delivery_id"])
    assert not (await talk.peer_unread(actor("bob"), reference_only=True))["messages"]
    reply = await talk.send(sender="bob", recipient="alice", text="Exact peer reply.")
    assert native_text(reply) == "Exact peer reply."
    history = await talk.peer_history(sent["conversation_id"], OPERATOR, max_bytes=32768)
    assert [native_text(item) for item in history["messages"]] == [text, "Exact peer reply."]


async def test_invalid_literature_reply_has_no_source_side_effect_then_corrected_bytes_succeed(comm):
    request = await comm.execute("literature.request", QUESTION, WORKER)
    supplied = comm.root / "reference.txt"
    supplied.write_text("Wrong edition", encoding="utf-8")
    response = {"request_id": request["id"], "outcome": "fulfilled", "source_path": str(supplied),
                "text": "", "message_id": "first"}
    with pytest.raises(HubError, match="Response must be nonempty"):
        await comm.execute("request.resolve", response)
    assert not await comm.list_literature()
    assert not (comm.root / "literature").exists()
    assert not (comm.root / ".task-based/literature").exists()
    supplied.write_text("Correct edition", encoding="utf-8")
    response.update(text="Here is the corrected source.", message_id="corrected")
    resolution = await comm.execute("request.resolve", response)
    entries = await comm.list_literature()
    assert len(entries) == 1 and entries[0]["id"] == resolution["source_id"]
    assert Path(entries[0]["source_path"]).read_text(encoding="utf-8") == "Correct edition"
    assert Path(entries[0]["index_path"]).read_text(encoding="utf-8").count("<!-- literature:") == 1
    original = (await comm.list_human_requests())[0]
    receipts = {path: path.read_bytes() for path in comm.root.rglob("*.md")}
    comm.clock = lambda: 1500
    assert await comm.execute("request.resolve", response) == resolution
    assert len(await comm.list_literature()) == 1
    assert (await comm.list_human_requests())[0] == original
    assert all(path.read_bytes() == body for path, body in receipts.items())
    with pytest.raises(HubError, match="immutable"):
        await comm.execute("request.resolve", {**response, "text": "Replacement answer.", "message_id": "replacement"})
    supplied.write_text("Another edition", encoding="utf-8")
    with pytest.raises(HubError, match="different terminal"):
        await comm.execute("request.resolve", {**response, "message_id": "another"})
    assert len(await comm.list_literature()) == 1
    assert await comm.db.call(lambda c: comm._external_wait_satisfied(c, request["wait_predicate"]))


async def test_literature_reply_conflicting_message_id_is_validated_before_registering(comm):
    request = await comm.execute("literature.request", QUESTION, WORKER)
    await comm.execute("message.send", {"worker_id": "alice", "text": "Existing message.", "message_id": "already-used"})
    supplied = comm.root / "reference.txt"
    supplied.write_text("Exact edition", encoding="utf-8")
    with pytest.raises(HubError, match="different content"):
        await comm.execute("request.resolve", {"request_id": request["id"], "source_path": str(supplied),
                           "text": "The source is available.", "message_id": "already-used"})
    assert not await comm.list_literature()
    assert not (comm.root / "literature").exists()


async def test_changed_file_during_resolution_does_not_publish_the_wrong_version(comm, monkeypatch):
    from formal_geyser.coordination import communications

    request = await comm.execute("literature.request", QUESTION, WORKER)
    supplied = comm.root / "reference.txt"
    supplied.write_text("Original edition", encoding="utf-8")
    fingerprint = communications.fingerprint
    def changed_after_fingerprint(path):
        observed = fingerprint(path)
        supplied.write_text("Changed edition", encoding="utf-8")
        return observed
    monkeypatch.setattr(communications, "fingerprint", changed_after_fingerprint)
    payload = {"request_id": request["id"], "source_path": str(supplied),
               "text": "Here is the source.", "message_id": "response"}
    with pytest.raises(HubError, match="changed before importing"):
        await comm.execute("request.resolve", payload)
    assert not await comm.list_literature() and not (comm.root / "literature").exists()
    monkeypatch.setattr(communications, "fingerprint", fingerprint)
    await comm.execute("request.resolve", payload)
    assert len(await comm.list_literature()) == 1
