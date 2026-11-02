"""Actual worker file delivery stays private and supports bounded text paging."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.material_files import MaterialFiles
from formal_geyser.coordination.models import (
    Actor,
    Command,
    Limits,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime import ScriptedHarness, WorkerRunner
from formal_geyser.coordination.storage import canonical

MARKER = "private-spill-marker"
PRIVATE_TEXT = (MARKER + ": mathematical text α ≤ β, ∀ x; \"quoted\".\n") * 24
BUDGET = 512


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("pf")


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             Actor(role=Role.OPERATOR))


@pytest.fixture
async def private_files(tmp_path, monkeypatch):
    resources = tmp_path / "readable-resources"
    resources.mkdir()
    hub = Hub(tmp_path, config=ProjectConfig(
        limits=Limits(packet_bytes=BUDGET, delta_bytes=256),
        environment={"resource_roots": {"fixture": str(resources)}}))
    try:
        task = await command(hub, "task.publish", {"contract": {
            "title": "Private file delivery", "objective": "Exercise synthetic transport only."}})
        run = await command(hub, "run.start")
        worker = await command(hub, "worker.register")
        attempt = await command(hub, "task.claim", {
            "task_id": task["id"], "revision": 1, "worker_id": worker["id"], "run_id": run["id"]})
        original_put = hub.blobs.put

        def forbid_private_blob(body):
            assert MARKER.encode() not in body, "Private tool text reached the shared blob store"
            return original_put(body)

        monkeypatch.setattr(hub.blobs, "put", forbid_private_blob)
        yield SimpleNamespace(hub=hub, worker=worker, attempt=attempt, run=run,
                              runner=WorkerRunner(hub, ScriptedHarness),
                              workspace=hub.workspace(worker["id"]), resources=resources)
    finally:
        await hub.close()


async def read_all(case, operation, arguments, expected, *, runner=None):
    runner = runner or case.runner
    offset = 0
    fragments = []
    for _ in range(len(expected) + 1):
        page = await runner._tool(case.attempt, "private-thread", "taskhub_call", {
            "operation": operation, "arguments": {**arguments, "offset": offset}}, uuid4().hex)
        assert len(canonical(page).encode("utf-8")) <= BUDGET
        assert "material_path" not in page, "A page became another unread material reference"
        assert isinstance(page.get("text"), str)
        assert page["offset"] == offset
        assert page["offset_unit"] == "Unicode code points"
        assert page["next_offset"] == offset + len(page["text"])
        fragments.append(page["text"])
        offset = page["next_offset"]
        assert page["has_more"] is (offset < len(expected))
        if not page["has_more"]:
            break
        assert page["text"], "Bounded paging must make progress"
    else:
        pytest.fail("Reading private material did not terminate")
    assert "".join(fragments) == expected


def assert_no_shared_private_text(case):
    for item in case.hub.blobs.root.glob("*/*"):
        if item.is_file():
            assert MARKER.encode() not in item.read_bytes()


@pytest.mark.parametrize("location", ["note", "material"])
async def test_actual_workspace_reader_keeps_private_pages_direct_and_within_budget(private_files, location):
    case = private_files
    if location == "note":
        path = ".private/notes/transport.txt"
        target = case.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PRIVATE_TEXT.encode("utf-8"))
        expected = PRIVATE_TEXT
    else:
        reference = MaterialFiles(case.workspace).json({"private_text": PRIVATE_TEXT})
        path = reference["material_path"]
        expected = (case.workspace / path).read_text(encoding="utf-8")
    await read_all(case, "workspace_read", {"path": path}, expected)
    assert_no_shared_private_text(case)


async def test_resource_reader_also_pages_by_utf8_bytes_without_recursive_spills(private_files):
    case = private_files
    target = case.resources / "reference.txt"
    target.write_bytes(PRIVATE_TEXT.encode("utf-8"))
    await read_all(case, "resource_read", {"root": "fixture", "path": "reference.txt"},
                   PRIVATE_TEXT)
    assert_no_shared_private_text(case)


async def test_generic_private_overflow_is_owned_file_and_native_reader_survives_runner_restart(private_files):
    case = private_files
    result = {"status": "private", "data": {"text": PRIVATE_TEXT}, "id": "owned-diagnostic"}
    expected = canonical(result)
    delivered = await case.runner._bound_tool_result(result, BUDGET, worker_id=case.worker["id"])
    assert len(canonical(delivered).encode()) <= BUDGET
    assert delivered["material_path"].startswith(".private/material/")
    target = case.workspace / delivered["material_path"]
    assert target.read_bytes() == expected.encode()
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    assert sha == delivered["material_sha256"]
    assert not (case.hub.blobs.root / sha[:2] / sha).exists()
    restarted = WorkerRunner(case.hub, ScriptedHarness)
    await read_all(case, "workspace_read", {"path": delivered["material_path"]}, expected, runner=restarted)
    assert_no_shared_private_text(case)


async def test_internal_diagnostic_receipt_references_its_owned_file_and_replays_exactly(private_files):
    case = private_files
    args = {"kind": "tool.diagnostic", "data": {"text": PRIVATE_TEXT}}
    request = Command(name="record.private", key="save-diagnostic", payload=args)
    actor = case.runner._worker_actor(case.attempt)
    receipt = await case.hub.execute(request, actor)
    replay = await case.hub.execute(request, actor)
    assert receipt == replay
    # The reply refers to the native file; the private body is never copied into it.
    assert receipt["status"] == "private" and "data" not in receipt
    assert PRIVATE_TEXT not in canonical(receipt)
    bounded = await case.runner._bound_tool_result(receipt, BUDGET, worker_id=case.worker["id"])
    assert len(canonical(bounded).encode()) <= BUDGET
    record = await case.hub.native_record(receipt["id"], actor)
    assert record["status"] == "private" and record["data"] == args["data"]
    assert await case.hub.list_notes() == []
    assert_no_shared_private_text(case)


async def test_a_workers_fetch_and_inspect_read_files_on_a_read_connection(private_files, monkeypatch):
    # In the Linux pilot a worker's fetch held the coordination thread 2 s on average while the
    # disk was busy; workers fetched some 230 records an hour.
    import threading

    from formal_geyser.coordination import native
    case = private_files
    actor = case.runner._worker_actor(case.attempt)
    receipt = await case.hub.execute(Command(name="record.private", key="read-here", payload={
        "kind": "tool.diagnostic", "data": {"text": "exact diagnostic"}}), actor)
    threads, original = [], native.NativeWorkflow._record_body

    def counted(self, c, record, **options):
        threads.append(threading.current_thread().name)
        return original(self, c, record, **options)
    monkeypatch.setattr(native.NativeWorkflow, "_record_body", counted)
    assert (await case.hub.native_record(receipt["id"], actor))["data"] == {"text": "exact diagnostic"}
    assert threads and all(name.startswith("taskhub-read-") for name in threads)
    reads = []
    original_task = native.NativeWorkflow._task_contract

    def task_contract(self, c, row):
        reads.append(threading.current_thread().name)
        return original_task(self, c, row)
    monkeypatch.setattr(native.NativeWorkflow, "_task_contract", task_contract)
    assert (await case.hub.task(case.attempt["task_id"]))["id"] == case.attempt["task_id"]
    assert reads and all(name.startswith("taskhub-read-") for name in reads)
    # A record moved between the snapshot and its file read is read again on a newer snapshot.
    failures = [OSError("moved meanwhile")]

    def once_moved(self, c, record, **options):
        if failures:
            raise failures.pop()
        return original(self, c, record, **options)
    monkeypatch.setattr(native.NativeWorkflow, "_record_body", once_moved)
    assert (await case.hub.native_record(receipt["id"], actor))["id"] == receipt["id"]


async def test_a_private_records_file_is_written_before_its_transaction(private_files, monkeypatch):
    # In the Linux pilot the fsync of this small file inside the transaction held the coordination
    # thread for up to 113 s while the disk wrote back other work.
    import os
    import threading

    from formal_geyser.coordination import content
    case = private_files
    actor = case.runner._worker_actor(case.attempt)
    records = case.hub.workspace(case.worker["id"]) / ".private" / "notes" / "records"
    fsyncs, original = [], os.fsync

    def counted(descriptor):
        fsyncs.append(threading.current_thread().name)
        return original(descriptor)
    monkeypatch.setattr(content.os, "fsync", counted)
    args = {"kind": "tool.diagnostic", "data": {"text": "exact diagnostic"}}
    receipt = await case.hub.execute(Command(name="record.private", key="early", payload=args), actor)
    assert fsyncs and not any(name.startswith("taskhub-db") for name in fsyncs)  # Before the transaction.
    assert sorted(path.name for path in records.iterdir()) == [receipt["id"] + ".json"]
    assert (await case.hub.native_record(receipt["id"], actor))["data"] == args["data"]
    # A replay keeps the first record; a refused record leaves no file behind.
    assert await case.hub.execute(Command(name="record.private", key="early", payload=args), actor) == receipt
    with pytest.raises(HubError):
        await case.hub.execute(Command(name="record.private", key="refused", payload={
            "kind": "tool.diagnostic", "data": args["data"], "input_refs": ["missing-artifact"]}), actor)
    assert sorted(path.name for path in records.iterdir()) == [receipt["id"] + ".json"]


async def test_retired_private_note_command_cannot_create_a_record(private_files):
    case = private_files
    args = {"kind": "note.private", "data": {"text": PRIVATE_TEXT}}
    direct = await case.runner._tool(case.attempt, "private-thread", "taskhub_private",
                                     args, "retired-note")
    assert direct["code"] == "UNKNOWN_GATEWAY"
    gateway = await case.runner._tool(case.attempt, "private-thread", "taskhub_call", {
        "operation": "private", "arguments": args}, "retired-operation")
    assert gateway["code"] == "UNKNOWN_OPERATION"
    assert await case.hub.list_artifacts() == []
    assert_no_shared_private_text(case)


async def test_retired_reader_cannot_dereference_historical_shared_blobs(private_files):
    case = private_files
    historical = "Historical shared text with mathematical notation α ≤ β.\n" * 12
    sha = case.hub.blobs.put(historical.encode())
    direct = await case.runner._tool(case.attempt, "private-thread", "taskhub_read",
                                     {"sha": sha}, "historical-read")
    assert direct["code"] == "UNKNOWN_GATEWAY"
    gateway = await case.runner._tool(case.attempt, "private-thread", "taskhub_call", {
        "operation": "read", "arguments": {"sha": sha}}, "historical-operation")
    assert gateway["code"] == "UNKNOWN_OPERATION"
    assert case.hub.blobs.get(sha).decode() == historical
    assert_no_shared_private_text(case)


async def test_private_file_paths_enforce_current_worker_fence(private_files):
    case = private_files
    reference = MaterialFiles(case.workspace).json({"text": PRIVATE_TEXT})
    stale = {**case.attempt, "fencing_token": case.attempt["fencing_token"] + 1}
    with pytest.raises(HubError) as denied:
        await case.runner._tool(stale, "private-thread", "taskhub_call", {
            "operation": "workspace_read", "arguments": {"path": reference["material_path"]}}, "stale-read")
    assert denied.value.code == "STALE_BINDING"
    assert_no_shared_private_text(case)


async def test_private_material_path_cannot_read_another_workers_file(private_files):
    case = private_files
    reference = MaterialFiles(case.workspace).json({"text": PRIVATE_TEXT})
    task = await command(case.hub, "task.publish", {"contract": {
        "title": "Other private reader", "objective": "Exercise worker isolation."}})
    worker = await command(case.hub, "worker.register")
    other = await command(case.hub, "task.claim", {
        "task_id": task["id"], "revision": 1, "worker_id": worker["id"], "run_id": case.run["id"]})
    with pytest.raises((FileNotFoundError, HubError)):
        await case.runner._tool(other, "other-thread", "taskhub_call", {
            "operation": "workspace_read", "arguments": {"path": reference["material_path"]}}, "not-your-file")
    assert not (case.hub.workspace(worker["id"]) / reference["material_path"]).exists()
    assert_no_shared_private_text(case)


async def test_native_reader_reports_current_private_file_bytes_and_hash(private_files):
    case = private_files
    reference = MaterialFiles(case.workspace).json({"text": PRIVATE_TEXT})
    (case.workspace / reference["material_path"]).write_text("Modified transport copy", encoding="utf-8")
    result = await case.runner._tool(case.attempt, "private-thread", "taskhub_call", {
        "operation": "workspace_read", "arguments": {"path": reference["material_path"]}}, "changed-private-file")
    assert result["text"] == "Modified transport copy"
    assert result["source_sha256"] != reference["material_sha256"]
    assert result["source_sha256"] == hashlib.sha256(b"Modified transport copy").hexdigest()
    assert_no_shared_private_text(case)
