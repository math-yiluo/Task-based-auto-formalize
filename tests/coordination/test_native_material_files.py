"""Exact file delivery, without model calls or arbitrary hash dereferencing."""
from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.content import ContentError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.material_files import MaterialFiles
from formal_geyser.coordination.models import (
    Actor,
    Command,
    Limits,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime import ToolResult, WorkerRunner
from formal_geyser.coordination.runtime.files import ScopedFiles
from formal_geyser.coordination.storage import canonical


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             actor or Actor(role=Role.OPERATOR))


@pytest.fixture
async def case(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig(limits=Limits(packet_bytes=8192, delta_bytes=2048)))
    try:
        secret = b"Private content must not be resolved from an arbitrary payload key."
        secret_sha = hub.blobs.put(secret)
        context = "# Exact task context\n" + "A source convention with Unicode: alpha = \u03b1.\n" * 250
        task = await command(hub, "task.publish", {"contract": {
            "title": "File delivery fixture", "objective": "Inspect the exact test context.",
            "context": context, "payload": {"material_sha256": secret_sha, "requires_read": True}}})
        run = await command(hub, "run.start")
        worker = await command(hub, "worker.register")
        attempt = await command(hub, "task.claim", {"task_id": task["id"], "revision": 1,
            "worker_id": worker["id"], "run_id": run["id"]})
        actor = Actor(role=Role.WORKER, worker_id=worker["id"], attempt_id=attempt["id"],
                      fencing_token=attempt["fencing_token"])
        yield SimpleNamespace(hub=hub, task=task, run=run, worker=worker, attempt=attempt,
                              actor=actor, context=context, secret=secret)
    finally:
        await hub.close()


async def test_context_files_are_written_after_its_transaction_off_the_coordination_thread(case, monkeypatch):
    from formal_geyser.coordination import material_files
    from formal_geyser.coordination.storage import BlobStore
    threads = []
    write, put = material_files._write_new, BlobStore.put
    monkeypatch.setattr(material_files, "_write_new",
                        lambda path, body: threads.append(threading.current_thread().name) or write(path, body))
    monkeypatch.setattr(BlobStore, "put", lambda self, body: threads.append(threading.current_thread().name) or put(self, body))
    packet = await case.hub.context(case.worker["id"])
    assert threads and not any(name.startswith("taskhub-db") for name in threads)
    path = case.hub.workspace(case.worker["id"]) / packet["task"]["context_material_path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == packet["task"]["context_material_sha256"]
    sha = await case.hub.db.read(lambda c: c.execute(
        "SELECT body_sha FROM deliveries WHERE id=?", (packet["packet_id"],)).fetchone()[0])
    assert json.loads(case.hub.blobs.get(sha)) == packet


async def test_large_context_and_checkpoint_are_exact_readable_files_with_receipts(case):
    checkpoint = {"summary": "Checkpoint body. " * 400, "next_action": "Continue the current task.", "input_refs": []}
    await command(case.hub, "task.checkpoint", {"checkpoint": checkpoint}, case.actor)
    packet = await case.hub.context(case.worker["id"])
    workspace = case.hub.workspace(case.worker["id"])
    path = workspace / packet["task"]["context_material_path"]
    assert json.loads(path.read_text(encoding="utf-8")) == case.context
    assert hashlib.sha256(path.read_bytes()).hexdigest() == packet["task"]["context_material_sha256"]
    assert json.loads((workspace / packet["checkpoint"]["material_path"]).read_text(encoding="utf-8")) == checkpoint
    assert json.loads((workspace / packet["task"]["contract_material_path"]).read_text(encoding="utf-8"))["context"] == case.context
    assert "taskhub_read" not in packet["material_access"]
    assert "material_path" not in packet["task"]["payload"]
    assert all(case.secret not in item.read_bytes() for item in (workspace / ".private/material").glob("*.json"))
    replay = await case.hub.context(case.worker["id"])
    assert replay["updates"] == packet["updates"]
    await command(case.hub, "context.ack", {"worker_id": case.worker["id"], "packet_id": packet["packet_id"]}, case.actor)
    assert not (await case.hub.context(case.worker["id"]))["updates"]
    assert all(not item["path"].startswith(".private/") for item in case.hub.blobs.get_json(
        case.hub._candidate_snapshot(case.worker["id"]))["files"])


async def test_large_tool_reply_uses_native_file_without_sha_pagination(case):
    runner = WorkerRunner(case.hub)
    result = await runner._tool(case.attempt, "fixture-thread", "taskhub_call", {
        "operation": "inspect", "arguments": {"task_id": case.task["id"]}}, "read-full-task")
    workspace = case.hub.workspace(case.worker["id"])
    assert len(canonical(result).encode()) <= 8192
    body = (workspace / result["material_path"]).read_bytes()
    assert json.loads(body)["contract"]["context"] == case.context
    assert hashlib.sha256(body).hexdigest() == result["material_sha256"]
    assert case.secret not in body


async def test_large_event_payload_remains_exact_and_acknowledges_only_delivery(case):
    reason = "Recorded observation, with all details retained. " * 80
    await case.hub.db.call(lambda c: case.hub._event(c, "task.observation", case.task["id"],
        {"reason": reason}, case.task["id"], case.worker["id"]), write=True)
    packet = await case.hub.context(case.worker["id"])
    workspace = case.hub.workspace(case.worker["id"])
    challenge = next(item for item in packet["updates"] if item["kind"] == "task.observation")
    payload = challenge["payload"]
    assert json.loads((workspace / payload["material_path"]).read_text())["reason"] == reason
    replay = await case.hub.context(case.worker["id"])
    assert challenge in replay["updates"]


async def test_receipt_bearing_private_results_are_not_rewritten_or_blob_copied(case, monkeypatch):
    runner = WorkerRunner(case.hub)
    sent = []

    async def acknowledge():
        sent.append(True)

    result = ToolResult({"messages": ["Private fixture. " * 1000]}, after_send=acknowledge)
    monkeypatch.setattr(case.hub.blobs, "put", lambda *args: pytest.fail("Private receipt body copied to shared blob"))
    bounded = await runner._bound_tool_result(result, 512, worker_id=case.worker["id"])
    assert bounded is result and not sent
    await bounded.after_send()
    assert sent == [True]


def test_material_files_repair_a_modified_copy_and_do_not_leave_partial_writes(tmp_path, monkeypatch):
    material = MaterialFiles(tmp_path)
    body = canonical({"exact": "value\n" * 400}).encode()
    reference = material.write(body)
    target = tmp_path / reference["material_path"]
    target.write_text("Modified disposable copy", encoding="utf-8")
    assert material.write(body) == reference and target.read_bytes() == body

    def interrupted(*args):
        raise OSError("Synthetic interrupted atomic publication")

    monkeypatch.setattr("formal_geyser.coordination.material_files.os.replace", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        material.json({"different": "body"})
    assert target.read_bytes() == body
    assert not list(target.parent.glob(".write-*"))


def test_material_files_refuse_linked_directories(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / ".private").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This test host cannot create directory links")
    with pytest.raises(ContentError, match="Linked"):
        MaterialFiles(workspace).json({"secret": "never write through a linked directory"})
    assert not list(outside.iterdir())


def test_a_material_path_longer_than_260_characters_is_readable(tmp_path):
    workspace = tmp_path / ("long-workspace-" * 6) / ("nested-" * 9)
    workspace.mkdir(parents=True)
    body = {"exact": "Unicode stays intact: \u03b1"}
    reference = MaterialFiles(workspace).json(body)
    assert len(str(workspace / reference["material_path"])) > 260
    page = ScopedFiles(workspace).read(reference["material_path"])
    assert json.loads(page["text"]) == body and not page["has_more"]
    listing = ScopedFiles(workspace).list(".private/material")
    assert listing["entries"][0]["path"] == reference["material_path"]
