"""Faults in advisory graphs cannot strand workers or monopolize coordination."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from formal_geyser.coordination import shared_dags
from formal_geyser.coordination.content import ContentError, NativeContentStore
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Role
from formal_geyser.coordination.shared_dags import DagFiles

from .test_gateway_runtime import call
from .test_gateway_runtime import project as project
from .test_shared_dags import current, proposed


async def test_new_calls_recover_committed_save_without_stale_pointer_loop(project, monkeypatch):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt)
    old = current(hub)
    point = DagFiles.point

    def blocked(*_):
        raise PermissionError("A reader temporarily holds current.json")

    monkeypatch.setattr(DagFiles, "point", blocked)
    saved = await call(runner, attempt, "dag_update", args, "first-save")
    assert saved["revision"] == 1 and current(hub) == old
    saved = await call(runner, attempt, "dag_update", args, "new-stale-call")
    assert saved["revision"] == 2  # Identical stale edits merge without a lost update.
    status = await call(runner, attempt, "dag_status", {}, "read-status")
    assert status["available"] is True and status["graphs"] == saved
    assert status["navigation"] == "pending"
    monkeypatch.setattr(DagFiles, "point", point)
    status = await call(runner, attempt, "dag_status", {}, "retry-navigation")
    assert status["navigation"] == "current" and current(hub) == saved
    assert (await call(runner, attempt, "dag_update", {**args, "expected_revision": 2}, "new-save"))["revision"] == 3


@pytest.mark.parametrize("damage", ["missing_body", "changed_body", "missing_manifest"])
async def test_damaged_graph_isolated_on_restart_and_preserved(project, damage):
    hub, task, attempt, runner = project
    saved = await call(runner, attempt, "dag_update", proposed(hub, attempt))
    damaged = Path(saved["manifest_path"] if damage == "missing_manifest" else saved["definitions"])
    damaged.chmod(0o666)
    if damage == "changed_body":
        damaged.write_text('{"nodes":[],"edges":[]}', encoding="utf-8")
        evidence = damaged.read_bytes()
    else:
        damaged.unlink()
        evidence = None
    await hub.close()
    reopened = Hub(hub.root)
    try:
        assert (await reopened.task(task["id"]))["id"] == task["id"]
        assert await reopened.list_tasks()
        status = await reopened.dag_status()
        assert status["available"] is False and status["graphs"] == saved
        assert "error" in status and "Preserve" in status["recovery"]
        assert (damaged.read_bytes() if damaged.exists() else None) == evidence
        assert await reopened.reconcile_dags() == saved
    finally:
        await reopened.close()


async def test_explicit_checked_replacement_can_repair_damaged_head(project):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt)
    saved = await call(runner, attempt, "dag_update", args)
    damaged = Path(saved["definitions"])
    damaged.chmod(0o666)
    damaged.write_bytes(b"damaged graph retained for diagnosis")
    assert (await hub.dag_status())["available"] is False
    replacement = await call(runner, attempt, "dag_update", {**args, "expected_revision": 1}, "restore")
    assert replacement["revision"] == 2
    assert (await hub.dag_status())["available"] is True
    assert damaged.read_bytes() == b"damaged graph retained for diagnosis"


async def test_validation_does_not_hold_sqlite_executor_or_writer_lock(project, monkeypatch):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt)
    entered, release = threading.Event(), threading.Event()
    validate = DagFiles.validate
    calls = []

    def slow_validate(files):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(10), "Test failed to release graph validation"
        return validate(files)

    monkeypatch.setattr(DagFiles, "validate", staticmethod(slow_validate))
    pending = asyncio.create_task(call(runner, attempt, "dag_update", args))
    other = sqlite3.connect(hub.db.path, timeout=0.05, autocommit=True)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        coordination_thread = await asyncio.wait_for(hub.db.call(lambda c: threading.get_ident()), 2)
        assert coordination_thread != calls[0]
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        release.set()
        other.close()
        saved = await pending
    assert calls and all(thread != coordination_thread for thread in calls)
    assert saved["revision"] == 1


async def test_slow_graph_install_does_not_block_task_reads_or_worker_authority(project, monkeypatch):
    hub, task, attempt, runner = project
    args = proposed(hub, attempt)
    entered, release = threading.Event(), threading.Event()
    tree = NativeContentStore._tree

    def slow_tree(self, directory, files, metadata, **kwargs):
        if metadata.get("kind") == "shared-dags":
            entered.set()
            assert release.wait(10), "Test failed to release graph installation"
        return tree(self, directory, files, metadata, **kwargs)

    monkeypatch.setattr(NativeContentStore, "_tree", slow_tree)
    pending = asyncio.create_task(call(runner, attempt, "dag_update", args))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await asyncio.wait_for(hub.task(task["id"]), 2))["id"] == task["id"]
        actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                      fencing_token=attempt["fencing_token"])
        await asyncio.wait_for(hub.authorize_worker(actor), 2)
    finally:
        release.set()
        assert (await pending)["revision"] == 1


def test_capture_bounds_combined_bytes_before_second_allocation(tmp_path, monkeypatch):
    source = tmp_path / ".private/dags/edit"
    source.mkdir(parents=True)
    empty = b'{"nodes":[],"edges":[]}'
    (source / "definitions.json").write_bytes(empty.ljust(700))
    (source / "proofs.json").write_bytes(empty.ljust(500))
    read = shared_dags._read
    limits, allocated = [], []

    def measured(path, *, max_bytes):
        limits.append(max_bytes)
        body = read(path, max_bytes=max_bytes)
        allocated.append(len(body))
        return body

    monkeypatch.setattr(shared_dags, "MAX_SNAPSHOT_BYTES", 1000)
    monkeypatch.setattr(shared_dags, "_read", measured)
    with pytest.raises(ContentError, match="byte limit"):
        DagFiles.capture(tmp_path, ".private/dags/edit")
    assert limits == [1000, 300] and allocated == [700]


async def test_gateway_large_graph_copy_edit_and_save_in_message_mode(project):
    hub, _, original, runner = project
    args = proposed(hub, original)
    source = hub.workspace(original["worker_id"]) / args["source_dir"]
    document = {"nodes": [{"id": "D", "title": "Original", "description": "x" * 1100000}], "edges": []}
    (source / "definitions.json").write_text(json.dumps(document), encoding="utf-8")
    saved = await call(runner, original, "dag_update", args)
    await hub.db.call(lambda c: c.execute("UPDATE attempts SET execution_mode='message_only' WHERE id=?", (original["id"],)), write=True)
    attempt = {**original, "execution_mode": "message_only"}
    for name in ("definitions", "proofs"):
        copied = await call(runner, attempt, "workspace_copy", {
            "root": "graphs", "source_path": Path(saved[name]).relative_to(hub.root / "graphs").as_posix(),
            "path": f".private/dags/large/{name}.json"}, f"copy-{name}")
        assert "sha256" in copied
    page = await call(runner, attempt, "workspace_read", {"path": ".private/dags/large/definitions.json"}, "read")
    edit = {"path": ".private/dags/large/definitions.json", "offset": page["text"].index("Original"),
            "delete_count": len("Original"), "text": "Revised", "expected_sha256": page["source_sha256"]}
    changed = await call(runner, attempt, "workspace_write", edit, "edit")
    with pytest.raises(ValueError, match="changed"):
        await call(runner, attempt, "workspace_write", edit, "edit")
    after = await call(runner, attempt, "workspace_read", {"path": edit["path"]}, "read-after")
    assert after["source_sha256"] == changed["sha256"]
    denied = await call(runner, attempt, "workspace_copy", {
        "root": "graphs", "source_path": "current.json", "path": ".private/notes/no.json"}, "denied")
    assert denied["code"] == "INVALID_ARGUMENTS"
    result = await call(runner, attempt, "dag_update", {
        "source_dir": ".private/dags/large", "expected_revision": 1, "summary": "Revise a large planning file."}, "save")
    assert json.loads(Path(result["definitions"]).read_bytes())["nodes"][0]["title"] == "Revised"
