"""Shared graph recovery serializes pointer installation with committed heads."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from formal_geyser.coordination.content import NativeContentStore
from formal_geyser.coordination.dag_check import GraphError
from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.coordination.shared_dags import DagFiles
from formal_geyser.coordination.storage import canonical

from .test_gateway_runtime import project as project


def graph_files(*, definition_depends_on_proof: bool = False) -> dict[str, bytes]:
    definitions = {
        "nodes": [{"id": "definition", "title": "Proposed definition"}],
        "edges": ([{"from": "definition", "to": "proof"}]
                  if definition_depends_on_proof else []),
    }
    proofs = {
        "nodes": [{"id": "proof", "title": "Proposed proof"}],
        "edges": [{"from": "proof", "to": "definition"}],
    }
    return {name: (canonical(document) + "\n").encode("utf-8") for name, document in (
        ("definitions.json", definitions), ("proofs.json", proofs),
    )}


async def test_dag_reconciliation_holds_ledger_lock_through_pointer_install(project, monkeypatch):
    hub, _, attempt, _ = project
    actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                  fencing_token=attempt["fencing_token"])
    source = hub.workspace(attempt["worker_id"]) / ".private" / "dags" / "recovery"
    source.mkdir(parents=True, exist_ok=True)
    files = graph_files()
    for name, body in files.items():
        (source / name).write_bytes(body)
    command = Command(name="dag.update", key="dag-lock-first", payload={
        "source_dir": ".private/dags/recovery", "expected_revision": 0,
        "summary": "Save the first shared graph revision for recovery.",
    })
    first = await hub.execute(command, actor)
    second = hub.dag_files.install(2, files, summary="The next committed graph head.")
    at_pointer = threading.Event()
    release_pointer = threading.Event()
    pending = None
    other = sqlite3.connect(hub.db.path, timeout=0.05, autocommit=True)
    original_point = DagFiles.point

    def blocked_point(self, reference):
        if reference["revision"] == first["revision"]:
            at_pointer.set()
            if not release_pointer.wait(10):
                raise AssertionError("The test did not release shared graph navigation")
        return original_point(self, reference)

    monkeypatch.setattr(DagFiles, "point", blocked_point)
    try:
        pending = asyncio.create_task(hub.reconcile_dags())
        assert await asyncio.to_thread(at_pointer.wait, 5), "Recovery never reached pointer install"

        # The old head has been read, but its pointer has not been installed yet.
        # A separate CLI/Hub connection cannot commit a newer head in that gap.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        release_pointer.set()
        assert await pending == first
        assert json.loads(Path(first["current_path"]).read_bytes()) == first

        # The same second connection can advance the head after installation.
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE native_dag_head SET revision=?,content_key=?,manifest_sha256=? WHERE id=1", (
            second["revision"], second["content_key"], second["manifest_sha256"]))
        other.execute("COMMIT")
        assert await hub.reconcile_dags() == second
        assert json.loads(Path(second["current_path"]).read_bytes()) == second

        # A replay returns its original immutable receipt, while reconciliation
        # uses the latest ledger head even when the original editable files vanished.
        for name in files:
            (source / name).unlink()
        assert await hub.execute(command, actor) == first
        assert json.loads(Path(second["current_path"]).read_bytes()) == second
        assert other.execute("SELECT revision FROM native_dag_head WHERE id=1").fetchone() == (2,)
    finally:
        release_pointer.set()
        if other.in_transaction:
            other.execute("ROLLBACK")
        other.close()
        if pending is not None:
            await pending


def test_storage_rejects_cross_graph_cycle_without_changing_current_pair(tmp_path):
    storage = DagFiles(NativeContentStore(tmp_path / ".task-based"))
    files = graph_files()
    current = storage.install(0, files, summary="Initial valid shared graph pair.")
    storage.point(current)
    pointer = Path(current["current_path"])
    original_pointer = pointer.read_bytes()
    original_entries = set(storage.root.iterdir())

    with pytest.raises(GraphError, match="Dependency cycle"):
        storage.install(1, graph_files(definition_depends_on_proof=True),
                        summary="An invalid pair with a cycle spanning both documents.")

    assert pointer.read_bytes() == original_pointer
    assert set(storage.root.iterdir()) == original_entries
    assert {name: (Path(current["root"]) / name).read_bytes() for name in files} == files
