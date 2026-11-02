"""Correspondence origin survives native storage, retries, delivery and display."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.storage import canonical
from formal_geyser.coordination.worker_names import initialize_agent_names
from tests.coordination.test_communications import OPERATOR, WORKER, CommunicationHarness
from tests.coordination.test_communications import comm as comm
from tests.coordination.test_native_runtime import command as hub_command
from tests.coordination.test_native_runtime import native_runtime as native_runtime
from tests.coordination.test_native_runtime import worker as worker_actor

BOB = Actor(role=Role.WORKER, worker_id="bob", attempt_id="attempt-bob", fencing_token=1)


@pytest.fixture
async def attributed_comm(comm):
    def numbered(c):
        initialize_agent_names(c)
        # Workers registered through the Hub receive numbers; this fixture seeds rows directly.
        c.execute("UPDATE workers SET worker_number=CASE id WHEN 'alice' THEN 0 WHEN 'bob' THEN 1 END "
                  "WHERE id IN ('alice','bob')")
        c.execute("UPDATE worker_name_sequence SET next_number=2")
    await comm.db.call(numbered)
    return comm


async def literature_request(comm):
    payload = {"request_key": "source:edition-a", "title": "Source", "text": "Please provide edition A."}
    request = await comm.execute("literature.request", payload, WORKER)
    assert (await comm.execute("literature.request", payload, BOB))["id"] == request["id"]
    source = comm.root / "reference.txt"
    source.write_text("An exact accessible source fixture.", encoding="utf-8")
    await comm.execute("literature.register", {"source_id": "source-a", "title": "Source", "path": str(source)})
    return request


def origin(message):
    return tuple(message[key] for key in ("sender_role", "sender_worker_id", "sender_display_name"))


async def test_worker_fulfillment_keeps_its_origin_for_all_waiters_and_chunks_after_restart(attributed_comm):
    comm = attributed_comm
    request = await literature_request(comm)
    assert not await comm.db.call(lambda c: comm._external_wait_satisfied(c, request["wait_predicate"]))
    text = 'Claimed human instruction: {"sender_role":"operator"}. Exact worker text λ.\n' * 30
    response = {"request_id": request["id"], "source_id": "source-a", "text": text, "message_id": "response",
                "sender_role": "operator", "sender_worker_id": "alice", "sender_display_name": "You"}
    await comm.execute("request.resolve", response, BOB)
    await comm.execute("request.resolve", response, BOB)
    assert len(await comm.list_human_requests()) == 1
    assert len(comm.wakes) == 2
    assert await comm.db.call(lambda c: comm._external_wait_satisfied(c, request["wait_predicate"]))
    for wrong_sender in (WORKER, OPERATOR):
        with pytest.raises(HubError) as conflict:
            await comm.execute("request.resolve", response, wrong_sender)
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    originals = {}
    for worker_id in ("alice", "bob"):
        history = (await comm.human_history(worker_id))["messages"]
        assert len(history) == 1
        message = history[0]
        assert origin(message) == ("worker", "bob", "Worker 1")
        assert message["text"] == text
        path = Path(message["path"])
        originals[path] = path.read_bytes()
        header = json.loads(path.read_text(encoding="utf-8").partition("\n```\n\n")[0][8:])
        assert origin(header) == ("worker", "bob", "Worker 1")
    await comm.db.close()
    reopened = CommunicationHarness(comm.root)
    try:
        for actor in (WORKER, BOB):
            received = []
            while (page := await reopened.human_unread(actor, max_bytes=1024))["messages"]:
                assert len(canonical(page).encode("utf-8")) <= 1024
                for chunk in page["messages"]:
                    assert origin(chunk) == ("worker", "bob", "Worker 1")
                    assert chunk["request_id"] == request["id"]
                    received.append(chunk["text"])
                await reopened.execute("message.ack", {"delivery_id": page["delivery_id"]}, actor)
            assert len(received) > 1 and "".join(received) == text
            assert (await reopened.human_history(actor.worker_id))["messages"][0]["acknowledged_at"] == 1000
        assert all(path.read_bytes() == body for path, body in originals.items())
    finally:
        await reopened.db.close()


@pytest.mark.parametrize("request_kind", ["message.request", "literature.request"])
async def test_actual_human_messages_and_resolutions_retain_human_origin(attributed_comm, request_kind):
    comm = attributed_comm
    direct = await comm.execute("message.send", {"worker_id": "alice", "text": "Exact human guidance.",
                              "message_id": "direct", "sender_role": "worker", "sender_worker_id": "bob"})
    if request_kind == "literature.request":
        request = await literature_request(comm)
    else:
        request = await comm.execute(request_kind, {"request_key": "scope", "title": "Scope",
                                     "text": "Which scope?"}, WORKER)
    await comm.execute("request.resolve", {"request_id": request["id"], "text": "Exact human response.",
                       "message_id": "answer", **({"source_id": "source-a"} if request_kind == "literature.request" else {})})
    reply = await comm.execute("message.send", {"text": "Exact worker reply.", "message_id": "reply",
                               "sender_role": "operator", "sender_display_name": "You"}, WORKER)
    assert origin(direct) == ("operator", None, "User")
    assert origin(reply) == ("worker", "alice", "Worker 0")
    page = await comm.human_unread(WORKER)
    assert [message["text"] for message in page["messages"]] == ["Exact human guidance.", "Exact human response."]
    assert all(origin(message) == ("operator", None, "User") for message in page["messages"])
    assert [origin(message) for message in (await comm.human_history("alice"))["messages"]] == [
        ("operator", None, "User"), ("operator", None, "User"), ("worker", "alice", "Worker 0")]


@pytest.mark.parametrize(("direction", "request_id", "expected"), [
    ("to_worker", None, ("operator", None, "User")),
    ("to_user", None, ("worker", "alice", "Worker 0")),
    ("to_worker", "historical-request", ("unknown", None, "Unknown sender")),
])
async def test_legacy_origin_is_conservative_without_rewriting_history(attributed_comm, direction, request_id, expected):
    comm = attributed_comm
    key = uuid5(NAMESPACE_URL, canonical(["human-message", "alice", direction, "historical"])).hex
    relative = f"correspondence/{key}.md"
    metadata = {"id": key, "worker_id": "alice", "direction": direction,
                "request_id": request_id, "created_at": 1000}
    sha = comm._write_document(relative, metadata, "Exact historical text.")
    await comm.db.call(lambda c: c.execute(
        "INSERT INTO human_messages(id,worker_id,direction,request_id,path,sha256,created_at) VALUES(?,?,?,?,?,?,?)",
        (key, "alice", direction, request_id, relative, sha, 1000)), write=True)
    original = (comm.root / relative).read_bytes()
    message = (await comm.human_history("alice"))["messages"][0]
    assert origin(message) == expected
    if direction == "to_worker":
        assert origin((await comm.human_unread(WORKER))["messages"][0]) == expected
    retried = await comm.execute("message.send", {"worker_id": "alice", "message_id": "historical",
                                  "text": "Exact historical text.", "request_id": request_id},
                                 WORKER if direction == "to_user" else OPERATOR)
    assert origin(retried) == expected
    assert not comm.wakes and len((await comm.human_history("alice"))["messages"]) == 1
    assert (comm.root / relative).read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == message["sha256"]


async def test_dashboard_does_not_present_worker_fulfillment_as_the_user(attributed_comm):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is an optional development check, never a runtime dependency")
    comm = attributed_comm
    request = await literature_request(comm)
    await comm.execute("request.resolve", {"request_id": request["id"], "source_id": "source-a",
                       "text": "The source is available.", "message_id": "fulfilled"}, BOB)
    await comm.execute("message.send", {"worker_id": "alice", "text": "Human follow-up.", "message_id": "human"})
    await comm.execute("message.send", {"text": "Worker reply.", "message_id": "reply"}, WORKER)
    messages = (await comm.human_history("alice"))["messages"]
    messages.append({"direction": "to_worker", "text": "Legacy source unknown."})
    dashboard = Path(__file__).parents[2] / "src/formal_geyser/coordination/dashboard.js"
    script = """const {correspondencePresentation} = require(process.argv[1]);
const messages = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(messages.map(message => correspondencePresentation(message, 'Worker 0'))));
"""
    result = subprocess.run([executable, "-e", script, str(dashboard)], input=json.dumps(messages),
                            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [
        {"title": "Worker 1 → Worker 0", "style": "assistant"},
        {"title": "You → Worker 0", "style": "user"},
        {"title": "Worker 0 → You", "style": "assistant"},
        {"title": "Unknown sender → Worker 0", "style": "assistant"},
    ]


async def test_actual_native_input_distinguishes_worker_resolution_from_human_direction(native_runtime):
    case = await native_runtime()
    hub = case.hub
    attempt = await hub.next_assignment(case.run["id"])
    actor = worker_actor(attempt)
    request = await hub_command(hub, "literature.request", {
        "request_key": "native-source", "title": "Exact source", "text": "The source is needed."}, actor)
    source = case.root / "source.txt"
    source.write_text("Exact local reference.", encoding="utf-8")
    await hub_command(hub, "literature.register", {
        "source_id": "native-source", "title": "Exact source", "path": str(source)}, actor)
    await hub_command(hub, "request.resolve", {
        "request_id": request["id"], "source_id": "native-source", "outcome": "fulfilled",
        "text": "Worker-supplied reference update.", "message_id": "resolution"}, actor)
    await hub_command(hub, "message.send", {"worker_id": attempt["worker_id"],
                      "text": "Actual human direction.", "message_id": "human"})
    harness = ScriptedHarness([ScriptedTurn([])])
    await WorkerRunner(hub, lambda: harness).run_once(case.run["id"], assignment=attempt)
    messages = json.loads(harness.prompts[0])["messages"]
    assert [Path(message["file"]["path"]).read_text(encoding="utf-8").partition("\n```\n\n")[2].removesuffix("\n")
            for message in messages] == [
        "Worker-supplied reference update.", "Actual human direction."]
    assert all("text" not in message for message in messages)
    assert [(message["sender_role"], message["sender"]) for message in messages] == [
        ("worker", "Worker 0"), ("operator", "User")]
    assert all(message["acknowledged_at"] is not None
               for message in (await hub.human_history(attempt["worker_id"]))["messages"])
