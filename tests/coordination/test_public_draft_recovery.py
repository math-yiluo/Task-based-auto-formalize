"""Draft navigation cannot regress behind a concurrently committed ledger head."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from formal_geyser.coordination.drafts import DraftFiles
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Command, ProjectConfig


async def test_draft_reconciliation_holds_ledger_lock_through_pointer_install(tmp_path, monkeypatch):
    hub = Hub(tmp_path, config=ProjectConfig())
    at_pointer = threading.Event()
    release_pointer = threading.Event()
    pending = None
    try:
        task = await hub.execute(Command(name="task.publish", key="draft-lock-task", payload={
            "contract": {"title": "Draft navigation fixture", "objective": "Exercise ledger isolation."}}))
        worker = await hub.execute(Command(name="worker.register", key="draft-lock-worker"))
        references = [hub.draft_files.install(
            task["id"], revision, task["revision"], worker["id"], worker["display_name"],
            f"Synthetic revision {revision}", {
                "Claim.lean": f"def claim : Prop := {'True' if revision == 1 else 'False'}\n".encode(),
                "Claim.md": b"An unreviewed proposition for the navigation fixture.\n",
            }) for revision in (1, 2)]
        first, second = references
        await hub.db.call(lambda c: c.execute("INSERT INTO native_draft_heads VALUES(?,?,?,?)", (
            task["id"], first["revision"], first["content_key"], first["manifest_sha256"])), write=True)
        original_point = DraftFiles.point

        def blocked_point(self, reference, **kwargs):
            if reference["revision"] == 1:
                at_pointer.set()
                if not release_pointer.wait(10):
                    raise AssertionError("The test did not release draft navigation")
            return original_point(self, reference, **kwargs)

        monkeypatch.setattr(DraftFiles, "point", blocked_point)
        pending = asyncio.create_task(hub.reconcile_drafts(task["id"]))
        assert await asyncio.to_thread(at_pointer.wait, 5), "Reconciliation never reached pointer install"

        # A second CLI/Hub connection must not commit a newer head after the
        # first connection reads the old head but before it points current.json.
        other = sqlite3.connect(hub.db.path, timeout=0.05, autocommit=True)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            if other.in_transaction:
                other.execute("ROLLBACK")
            other.close()
        release_pointer.set()
        await pending
        assert json.loads(Path(first["current_path"]).read_bytes()) == first

        # Once the pointer matches the old committed head, another connection
        # can advance the ledger and a later reconciliation publishes its head.
        other = sqlite3.connect(hub.db.path, autocommit=True)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("UPDATE native_draft_heads SET revision=?,content_key=?,manifest_sha256=? WHERE task_id=?", (
                second["revision"], second["content_key"], second["manifest_sha256"], task["id"]))
            other.execute("COMMIT")
        finally:
            if other.in_transaction:
                other.execute("ROLLBACK")
            other.close()
        await hub.reconcile_drafts(task["id"])
        assert json.loads(Path(second["current_path"]).read_bytes()) == second
    finally:
        release_pointer.set()
        if pending is not None:
            await pending
        await hub.close()
