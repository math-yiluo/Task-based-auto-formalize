from __future__ import annotations

import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.storage import canonical
from tests.coordination.lifecycle_helpers import (
    assignment_decision,
    open_collaboration,
    select_review,
)
from tests.coordination.publication_helpers import file_payload, new_hub

OPERATOR = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


def worker_actor(attempt, **changes):
    return Actor(**{"role": Role.WORKER, "worker_id": attempt["worker_id"],
                    "attempt_id": attempt["id"], "fencing_token": attempt["fencing_token"], **changes})


async def quiet(hub, attempt):
    return await command(hub, "runtime.quiescent", {"worker_id": attempt["worker_id"],
                         "snapshot": hub.blobs.snapshot(hub.workspace(attempt["worker_id"]))}, RUNNER)


async def task(hub, title):
    return await command(hub, "task.publish", {"contract": {"title": title, "objective": "Check the exact stated result."}})


async def test_concurrent_numbers_are_stable_and_not_reused_after_retirement_or_deletion(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig(), clock=lambda: 1000)
    try:
        workers = await asyncio.gather(*(command(hub, "worker.register") for _ in range(12)))
        assert sorted(worker["worker_number"] for worker in workers) == list(range(12))
        numbered = await hub.list_workers()
        assert [worker["display_name"] for worker in numbered] == [f"Worker {number}" for number in range(12)]
        await command(hub, "worker.bind_session", {"worker_id": "Worker 0", "thread_id": "native-thread-unchanged"}, RUNNER)
        await hub.db.call(lambda c: c.execute("UPDATE workers SET state='retired',work_intention='retired' WHERE worker_number=10"), write=True)
        await hub.db.call(lambda c: c.execute("DELETE FROM workers WHERE worker_number=11"), write=True)
        original = {worker["id"]: worker["display_name"] for worker in await hub.list_workers()}
    finally:
        await hub.close()
    restored = Hub(tmp_path, config=ProjectConfig())
    try:
        assert await restored.db.call(restored._worker_display_names) == original
        assert (await restored.worker("Worker 0"))["thread_id"] == "native-thread-unchanged"
        assert (await restored.worker("Worker 10"))["state"] == "retired"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await restored.db.call(lambda c: c.execute("UPDATE workers SET worker_number=99 WHERE worker_number=0"), write=True)
        next_worker = await command(restored, "worker.register")
        assert next_worker["worker_number"] == 12
        assert next_worker["display_name"] == "Worker 12"
        with pytest.raises(HubError, match="reserved"):
            await command(restored, "worker.register", {"worker_id": "Worker 2"})
        with pytest.raises(HubError) as missing:
            await restored.resolve_worker_id("Worker 11")
        assert missing.value.code == "NOT_FOUND"
        with pytest.raises(HubError) as oversized:
            await restored.resolve_worker_id("Worker " + "9" * 100)
        assert oversized.value.code == "NOT_FOUND"
    finally:
        await restored.close()


async def test_decisions_do_not_consume_names_and_reviewers_use_their_own_sequence(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        root = await task(hub, "Task 0")
        run = await command(hub, "run.start")
        first = await hub.next_assignment(run["id"])
        await open_collaboration(hub, root["id"])
        assert len(await hub.list_workers()) == 1
        idle = await command(hub, "worker.register")
        assert idle["display_name"] == "Worker 1"
        # The collaborator opening waits for one assignment decision. Its owner, the
        # Dispatcher, is a reserved system identity outside both numbered sequences.
        decision = await assignment_decision(hub, run["id"])
        assert decision["source_task_id"] == root["id"]
        assert [item["worker_id"] for item in decision["candidates"]["workers"]] == [idle["id"]]
        dispatcher = await hub.worker(decision["source_worker_id"])
        assert dispatcher["display_name"] == "Dispatcher"
        assert dispatcher["worker_number"] is None and dispatcher["reviewer_number"] is None
        with pytest.raises(HubError, match="reserved"):
            await command(hub, "worker.register", {"worker_id": "Dispatcher"})
        assert sorted(worker["display_name"] for worker in await hub.list_workers()) == [
            "Dispatcher", "Worker 0", "Worker 1"]
        child = (await hub.complete_assignment(decision["id"], "Worker 1"))["assignment"]
        assert child["worker_id"] == idle["id"] and child["task_id"] == root["id"]
        assert child["worker_id"] != first["worker_id"]
        assert (await hub.worker(child["worker_id"]))["display_name"] == "Worker 1"
        candidate = await command(hub, "artifact.stage", file_payload(
            hub, child, data={"result": "ready"}), worker_actor(child))
        registered = await command(hub, "worker.register")
        assert registered["display_name"] == "Worker 2"
        reviewer = await select_review(hub, candidate["review_task_id"], run["id"])
        identity = await hub.worker(reviewer["worker_id"])
        assert identity["display_name"] == "Reviewer 1"
        assert identity["reviewer_number"] == 1 and identity["worker_number"] is None
        assert await hub.resolve_worker_id("reviewer 1") == reviewer["worker_id"]
        page = await hub.task_page()
        review_task = next(item for item in page["items"] if item["id"] == candidate["review_task_id"])
        assert review_task["collaboration"]["members"][0]["display_name"] == "Reviewer 1"
        named_message = await hub.db.call(lambda c: hub._peer_named_row(c, {
            "sender_id": reviewer["worker_id"], "recipient_id": child["worker_id"], "text": "Quoted Worker 99."}))
        assert named_message["sender_display_name"] == "Reviewer 1"
        assert named_message["recipient_display_name"] == "Worker 1"
        assert named_message["text"] == "Quoted Worker 99."
        review = reviewer["task"]["collaboration"]["review"]
        report = {key: review[key] for key in ("candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")}
        await command(hub, "review.reject", {**report, "rationale": "The claimed result has no supporting calculation.",
                      "suggestions": ["Supply the missing calculation."], "evidence": ["Inspected the submitted result."], "qualifications": []}, worker_actor(reviewer))
        await quiet(hub, reviewer)
        assert (await hub.worker("Reviewer 1"))["work_intention"] == "retired"
        assert (await command(hub, "worker.register"))["display_name"] == "Worker 3"
        workers = await hub.list_workers()
        assert sorted(worker["worker_number"] for worker in workers if worker["kind"] == "ordinary") == [0, 1, 2, 3]
        assert [worker["reviewer_number"] for worker in workers if worker["kind"] == "review"] == [1]
        assert [(worker["display_name"], worker["worker_number"], worker["reviewer_number"])
                for worker in workers if worker["kind"] == "dispatcher"] == [("Dispatcher", None, None)]
        # The recruitment and assignment decisions consumed no names; the review needed no decision.
        assert await hub.db.call(lambda c: dict(c.execute(
            "SELECT kind,count(*) FROM helper_intents GROUP BY kind").fetchall())) == {"recruitment": 1, "assignment": 1}
        assert hub.workspace(reviewer["worker_id"]).parent.name == reviewer["worker_id"]
    finally:
        await hub.close()


async def test_review_numbers_are_independent_concurrent_immutable_and_not_reused(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig(), clock=lambda: 1000)
    try:
        registered = await asyncio.gather(*(command(hub, "worker.register", {"kind": kind})
                                           for kind in ["review", "ordinary"] * 6))
        ordinary = [item for item in registered if item["kind"] == "ordinary"]
        reviewers = [item for item in registered if item["kind"] == "review"]
        assert sorted(item["worker_number"] for item in ordinary) == list(range(6))
        assert all(item["reviewer_number"] is None for item in ordinary)
        assert sorted(item["reviewer_number"] for item in reviewers) == list(range(1, 7))
        assert all(item["worker_number"] is None for item in reviewers)
        for reviewer in reviewers:
            assert await hub.resolve_worker_id(reviewer["display_name"].lower()) == reviewer["id"]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await hub.db.call(lambda c: c.execute("UPDATE workers SET reviewer_number=99 WHERE reviewer_number=1"), write=True)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            await hub.db.call(lambda c: c.execute("UPDATE workers SET reviewer_number=NULL WHERE reviewer_number=1"), write=True)
        with pytest.raises(HubError, match="reserved"):
            await command(hub, "worker.register", {"worker_id": "Reviewer 1"})
        with pytest.raises(HubError) as oversized:
            await hub.resolve_worker_id("Reviewer " + "9" * 100)
        assert oversized.value.code == "NOT_FOUND"
        await hub.db.call(lambda c: c.execute("UPDATE workers SET state='retired',work_intention='retired' WHERE reviewer_number=5"), write=True)
        await hub.db.call(lambda c: c.execute("DELETE FROM workers WHERE reviewer_number=6"), write=True)
        names = await hub.db.call(hub._worker_display_names)
    finally:
        await hub.close()
    restarted = Hub(tmp_path, config=ProjectConfig())
    try:
        assert await restarted.db.call(restarted._worker_display_names) == names
        assert all(item["worker_number"] is None for item in await restarted.list_workers() if item["kind"] == "review")
        assert (await command(restarted, "worker.register"))["display_name"] == "Worker 6"
        assert (await command(restarted, "worker.register", {"kind": "review"}))["display_name"] == "Reviewer 7"
        assert (await restarted.worker("Reviewer 5"))["state"] == "retired"
        with pytest.raises(HubError) as missing:
            await restarted.resolve_worker_id("Reviewer 6")
        assert missing.value.code == "NOT_FOUND"
    finally:
        await restarted.close()


async def test_worker_and_reviewer_numbers_cannot_overlap_roles(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig())
    try:
        ordinary = await command(hub, "worker.register")
        reviewer = await command(hub, "worker.register", {"kind": "review"})
        assert ordinary["display_name"] == "Worker 0"
        assert reviewer["display_name"] == "Reviewer 1"
        with pytest.raises(HubError, match="does not exist"):
            await hub.resolve_worker_id("Worker 1")
        with pytest.raises(sqlite3.IntegrityError, match="disjoint"):
            await hub.db.call(lambda c: c.execute("UPDATE workers SET worker_number=1 WHERE id=?",
                                                 (reviewer["id"],)), write=True)
        with pytest.raises(sqlite3.IntegrityError, match="disjoint"):
            await hub.db.call(lambda c: c.execute("UPDATE workers SET reviewer_number=2 WHERE id=?",
                                                 (ordinary["id"],)), write=True)
    finally:
        await hub.close()


async def test_aliases_route_talk_and_coauthors_without_changing_fences_or_message_bodies(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        root = await task(hub, "Task 0")
        run = await command(hub, "run.start")
        first = await hub.next_assignment(run["id"])
        await open_collaboration(hub, root["id"])
        peer = await command(hub, "worker.register")
        second = await command(hub, "task.claim", {"task_id": root["id"], "revision": 1,
                               "worker_id": "Worker 1", "run_id": run["id"], "plan": "Check the second condition."})
        text = ("Preserve this quoted identity " + first["worker_id"] + " and Worker 99 verbatim.\n") * 12
        sent = await command(hub, "talk.send", {"recipient_id": "Worker 1", "message_id": "stable-message", "text": text}, worker_actor(first))
        again = await command(hub, "talk.send", {"recipient_id": peer["id"], "message_id": "stable-message", "text": text}, worker_actor(first))
        assert again == sent
        saved = json.loads(hub._peer_file(sent["conversation_id"], sent["id"]).read_text(encoding="utf-8"))
        assert saved["sender_id"] == first["worker_id"] and saved["recipient_id"] == peer["id"]
        assert saved["text"] == text
        history = await hub.peer_history(sent["conversation_id"], OPERATOR, worker_id="Worker 1")
        assert history["messages"][0]["sender_display_name"] == "Worker 0"
        assert history["messages"][0]["recipient_display_name"] == "Worker 1"
        presented = await hub.present_worker_data(history)
        assert presented["messages"][0]["sender_id"] == "Worker 0"
        assert presented["messages"][0]["text"] == text
        chunks = []
        for _ in range(100):
            page = await hub.peer_unread(worker_actor(second), worker_id="Worker 1", max_bytes=2048)
            assert len(canonical(page).encode("utf-8")) <= 2048
            if not page["messages"]:
                break
            assert page["messages"][0]["sender_display_name"] == "Worker 0"
            chunks.extend(message["text"] for message in page["messages"])
            await command(hub, "talk.ack", {"delivery_id": page["delivery_id"]}, worker_actor(second))
        assert "".join(chunks) == text
        with pytest.raises(HubError) as stale:
            await command(hub, "talk.send", {"recipient_id": "Worker 1", "message_id": "stale", "text": "Not authorized."},
                          worker_actor(first, fencing_token=first["fencing_token"] + 1))
        assert stale.value.code == "STALE_BINDING"
        observed = await hub.peer_unread(worker_actor(first), worker_id="Worker 1")
        assert observed["delivery_id"] is None  # Reading never acknowledges another recipient.
        candidate = await command(hub, "artifact.stage", file_payload(hub, first,
            data={"result": "both contributed"}, coauthors=["Worker 1"]), worker_actor(first))
        binding = (await hub.artifact(candidate["artifact_id"]))["candidate_binding"]
        assert binding["coauthors"] == [peer["id"]]
        packet = await hub.context(second["worker_id"])
        assert packet["display_name"] == "Worker 1" and packet["worker_number"] == 1
        members = (await hub.task(root["id"]))["collaboration"]["members"]
        assert {member["display_name"] for member in members} == {"Worker 0", "Worker 1"}
    finally:
        await hub.close()


async def test_projection_is_typed_and_keeps_operational_and_exact_evidence_values(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig())
    try:
        first, second = [await command(hub, "worker.register") for _ in range(2)]
        receipt = {"worker_id": first["id"], "hash": "unchanged"}
        original = {"worker_id": first["id"], "source_worker_id": second["id"], "coauthors": [second["id"]],
                    "task_id": first["id"], "artifact_id": first["id"], "thread_id": first["id"],
                    "workspace": "workers/" + first["id"], "text": first["id"], "fencing_token": 17,
                    "data": {"worker_id": first["id"]}, "receipt": receipt, "proof_json": canonical(receipt),
                    "record": {"producer": first["id"]}}
        before = canonical(original)
        projected = await hub.present_worker_data(original)
        assert projected["worker_id"] == "Worker 0"
        assert projected["source_worker_id"] == "Worker 1"
        assert projected["coauthors"] == ["Worker 1"]
        for key in set(original) - {"worker_id", "source_worker_id", "coauthors"}:
            assert projected[key] == original[key]
        assert canonical(original) == before
        resolved = await hub.resolve_worker_refs({"worker_id": "worker 0", "coauthors": ["Worker 1"], "data": {"worker_id": "Worker 99"}})
        assert resolved == {"worker_id": first["id"], "coauthors": [second["id"]], "data": {"worker_id": "Worker 99"}}
    finally:
        await hub.close()


async def test_tool_results_are_named_off_the_coordination_thread(tmp_path, monkeypatch):
    hub = Hub(tmp_path, config=ProjectConfig())
    try:
        worker = await command(hub, "worker.register")
        value = {"worker_id": worker["id"], "items": [{"author_id": worker["id"], "text": "unchanged"}]}
        expected = await hub.db.call(lambda c: hub._present_worker_data(c, value))
        assert expected["worker_id"] == "Worker 0" and expected["items"][0]["author_id"] == "Worker 0"

        async def refused(*args, **kwargs):
            raise AssertionError("Every tool result is presented: never on the coordination thread")
        monkeypatch.setattr(hub.db, "call", refused)
        assert await hub.present_worker_data(value) == expected
        monkeypatch.undo()
    finally:
        await hub.close()



async def test_display_names_are_read_once_and_follow_new_workers(tmp_path):
    # Every tool result is presented with every agent's name, read on a read connection: only
    # workers added since the last call are read, and a transaction's own uncommitted workers are
    # not remembered.
    hub = Hub(tmp_path, config=ProjectConfig())
    try:
        first = await command(hub, "worker.register")
        assert (await hub.db.read(hub._worker_display_names)) == {first["id"]: "Worker 0"}
        statements = []

        def read(c):
            c.set_trace_callback(statements.append)
            try:
                return hub._worker_display_names(c)
            finally:
                c.set_trace_callback(None)
        assert await hub.db.read(read) == {first["id"]: "Worker 0"}
        reads = [sql for sql in statements if "FROM workers" in sql]
        assert reads and all("WHERE rowid>" in sql or "count(*)" in sql for sql in reads)
        assert not any("WHERE rowid>0 " in sql for sql in reads)  # The read connection's last call is kept.

        def rolled_back(c):
            c.execute("SAVEPOINT probe")
            c.execute("INSERT INTO workers(id,worker_number,kind,created_at) VALUES('probe',99,'ordinary',0)")
            names = hub._worker_display_names(c)
            c.execute("ROLLBACK TO probe")
            c.execute("RELEASE probe")
            return names
        assert (await hub.db.call(rolled_back, write=True))["probe"] == "Worker 99"
        second = await command(hub, "worker.register")  # It may take the rolled-back row's rowid.
        names = await hub.db.read(hub._worker_display_names)
        assert names == {first["id"]: "Worker 0", second["id"]: "Worker 1"}
        await hub.db.call(lambda c: c.execute("DELETE FROM workers WHERE id=?", (second["id"],)), write=True)
        third = await command(hub, "worker.register")
        assert await hub.db.read(hub._worker_display_names) == {first["id"]: "Worker 0", third["id"]: "Worker 2"}
    finally:
        await hub.close()


@pytest.mark.parametrize("name,payload,role", [
    ("talk.send", {"recipient_id": "missing", "sender_id": "unknown", "message_id": "one", "text": "Forged."}, Role.OPERATOR),
    ("talk.send", {"recipient_id": "Worker 999", "message_id": "one", "text": "Forged."}, Role.OPERATOR),
    ("worker.bind_session", {"worker_id": "Worker 999", "thread_id": "forged"}, Role.OPERATOR),
    ("usage.record", {"worker_id": "Worker 999", "sample": {}}, Role.OPERATOR),
    ("runtime.abandon", {"worker_id": "Worker 999", "quiescent": True}, Role.OPERATOR),
    ("worker.recover", {"worker_id": "Worker 999"}, Role.RUNNER),
    ("message.send", {"worker_id": "Worker 999", "text": "Forged."}, Role.RUNNER),
])
async def test_forbidden_commands_reject_before_resolving_target_names(tmp_path, name, payload, role):
    hub = Hub(tmp_path, config=ProjectConfig())
    try:
        with pytest.raises(HubError) as forbidden:
            await command(hub, name, payload, Actor(role=role))
        assert forbidden.value.code == "FORBIDDEN"
        assert await hub.list_workers() == []
    finally:
        await hub.close()


async def test_stale_worker_fences_precede_unknown_identity_lookups(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig())
    try:
        await task(hub, "Task 0")
        run = await command(hub, "run.start")
        first = await hub.next_assignment(run["id"])
        stale = worker_actor(first, fencing_token=first["fencing_token"] + 1)
        for name, payload in [
            ("talk.send", {"recipient_id": "Worker 999", "message_id": "one", "text": "Stale."}),
            ("artifact.stage", {"coauthors": ["Worker 999"], "kind": "software.result", "data": {}}),
            ("artifact.submit", {"coauthors": ["Worker 999"], "kind": "software.result", "data": {}}),
            ("message.send", {"worker_id": "Worker 999", "message_id": "two", "text": "Stale."}),
            ("context.ack", {"worker_id": "Worker 999", "packet_id": "missing"}),
        ]:
            with pytest.raises(HubError) as expired:
                await command(hub, name, payload, stale)
            assert expired.value.code == "STALE_BINDING", name
        with pytest.raises(HubError) as inbox:
            await hub.peer_unread(stale, worker_id="Worker 999")
        assert inbox.value.code == "STALE_BINDING"
        assert (await hub.list_workers())[0]["runtime_stop_reason"] is None
    finally:
        await hub.close()
