"""Workspace refresh regressions over exact synthetic bytes, without models or Lean."""
import json
import sqlite3
import stat
import time
from types import SimpleNamespace

import pytest

from formal_geyser.coordination.content import ContentError
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.integration import RepositoryCoordination
from formal_geyser.coordination.native import NativeWorkflow
from formal_geyser.coordination.storage import BlobStore


class FixtureRepository(RepositoryCoordination, NativeWorkflow):
    def __init__(self, root):
        self.root = root
        self.blobs = BlobStore(root / "blobs")
        self.config = SimpleNamespace(domain="lean", repository=SimpleNamespace(enabled=True))
        self.head = "1" * 40
        self.snapshots = {}
        self._native_read_cache = {}
        self.clock = lambda: 1
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE repository_bases(worker_id TEXT PRIMARY KEY,base_commit TEXT,initialized_at INTEGER);
        """)

    def workspace(self, worker_id):
        path = self.root / worker_id
        path.mkdir(exist_ok=True)
        return path

    def _repository(self):
        return SimpleNamespace(head=lambda: self.head, snapshot=lambda head, blobs: self.snapshots[head])

    @property
    def head(self):
        return self._head

    @head.setter
    def head(self, value):
        self._head = value
        # Direct fixture calls model a fresh database read after a publication.
        if hasattr(self, "_native_read_cache"):
            self._native_read_cache.clear()

    def _require_recorded_repository_head(self, c, repository):
        return repository.head()

    def _repository_head_recorded(self, c, head):
        return head in self.snapshots

    def _candidate_repository(self, c, identity):
        return None

    def snapshot(self, files):
        return self.blobs.put_json({"files": [
            {"path": path, "sha256": self.blobs.put(body)} for path, body in sorted(files.items())]})


def seed_files(store, connection, worker_id):
    head = store._require_recorded_repository_head(connection, store._repository())
    prior = connection.execute("SELECT base_commit FROM repository_bases WHERE worker_id=?", (worker_id,)).fetchone()
    report = store._refresh_repository_files(worker_id, prior[0] if prior else None, head)
    connection.execute("INSERT INTO repository_bases VALUES(?,?,?) ON CONFLICT(worker_id) DO UPDATE SET base_commit=excluded.base_commit",
                       (worker_id, head, store.clock()))
    return report


@pytest.fixture
def store(tmp_path):
    store = FixtureRepository(tmp_path)
    yield store
    store.connection.close()


def test_refresh_uses_three_way_base_preserves_edits_and_exposes_conflicts(store):
    before = {"updated.md": b"base", "private.md": b"base", "conflict.md": b"base", "deleted.md": b"base"}
    store.snapshots[store.head] = store.snapshot(before)
    seed_files(store, store.connection, "worker")
    root = store.workspace("worker")
    (root / "private.md").write_bytes(b"private edit")
    (root / "conflict.md").write_bytes(b"local edit")
    (root / ".private/note.md").write_bytes(b"private note")
    store.head = "2" * 40
    store.snapshots[store.head] = store.snapshot({"updated.md": b"published", "private.md": b"base", "conflict.md": b"incoming", "new.md": b"new"})
    report = seed_files(store, store.connection, "worker")
    assert (root / "updated.md").read_bytes() == b"published"
    assert (root / "private.md").read_bytes() == b"private edit"
    assert (root / "conflict.md").read_bytes() == b"local edit"
    assert (root / ".private/note.md").read_bytes() == b"private note"
    assert not (root / "deleted.md").exists()
    assert (root / "new.md").read_bytes() == b"new"
    assert [item["path"] for item in report["conflicts"]] == ["conflict.md"]
    assert (root / report["conflicts"][0]["public_file"]).read_bytes() == b"incoming"
    assert seed_files(store, store.connection, "worker")["conflicts"] == report["conflicts"]
    with pytest.raises(HubError, match="conflicts"):
        store._bind_publication_repository(store.connection, {"worker_id": "worker"}, {"id": "unused"}, ["conflict.md"])
    (root / "conflict.md").write_bytes(b"resolved by worker")
    report["resolved_paths"] = ["conflict.md"]
    (root / ".private/repository-refresh.json").write_text(json.dumps(report), encoding="utf-8")
    assert not seed_files(store, store.connection, "worker")["conflicts"]
    assert (root / "conflict.md").read_bytes() == b"resolved by worker"
    seed_files(store, store.connection, "new-worker")
    assert (store.workspace("new-worker") / "conflict.md").read_bytes() == b"incoming"
    assert not (store.workspace("new-worker") / ".private/note.md").exists()


def test_refresh_preserves_locally_changed_file_deleted_from_public_tree(store):
    store.snapshots[store.head] = store.snapshot({"removed.md": b"base"})
    seed_files(store, store.connection, "worker")
    path = store.workspace("worker") / "removed.md"
    path.write_bytes(b"retain private changes")
    store.head = "2" * 40
    store.snapshots[store.head] = store.snapshot({})
    conflict = seed_files(store, store.connection, "worker")["conflicts"][0]
    assert conflict["public_sha256"] is None and "public_file" not in conflict
    assert path.read_bytes() == b"retain private changes"


def test_a_refresh_updates_a_file_the_worker_left_read_only(store):
    # `cp` out of the read-only published Library keeps the mode (Linux pilot, Worker 281).
    store.snapshots[store.head] = store.snapshot({"copied.md": b"base"})
    seed_files(store, store.connection, "worker")
    path = store.workspace("worker") / "copied.md"
    path.chmod(stat.S_IREAD)
    store.head = "2" * 40
    store.snapshots[store.head] = store.snapshot({"copied.md": b"published"})
    assert not seed_files(store, store.connection, "worker")["conflicts"]
    assert path.read_bytes() == b"published"


def test_a_refresh_reads_only_files_changed_since_the_last_snapshot(store, monkeypatch):
    from pathlib import Path as PathType

    from formal_geyser.coordination import storage

    store.snapshots[store.head] = store.snapshot({"kept.md": b"base", "edited.md": b"base", "updated.md": b"base",
                                                  "both.md": b"base"})
    seed_files(store, store.connection, "worker")
    root = store.workspace("worker").resolve()
    monkeypatch.setattr(storage, "SNAPSHOT_SETTLED_NANOSECONDS", 0)
    store.blobs.snapshot(root)  # A turn's end.
    time.sleep(0.05)  # A later write gets a later change time.
    (root / "edited.md").write_bytes(b"local edit")
    (root / "both.md").write_bytes(b"local edit")
    reads = []
    original = PathType.read_bytes

    def counted(path):
        if root in PathType(path).resolve().parents:
            reads.append(path.name)  # Workspace files only, not the blob store's.
        return original(path)
    monkeypatch.setattr(PathType, "read_bytes", counted)
    store.head = "2" * 40
    store.snapshots[store.head] = store.snapshot({"kept.md": b"base", "edited.md": b"base", "updated.md": b"new",
                                                  "both.md": b"new"})
    report = seed_files(store, store.connection, "worker")
    # Only a file the publication changed and the worker edited since its snapshot is read again:
    # not the files the publication left alone, edited or not, nor a changed one left unedited.
    assert reads == ["both.md"]
    assert (root / "edited.md").read_bytes() == b"local edit" and (root / "updated.md").read_bytes() == b"new"
    assert [item["path"] for item in report["conflicts"]] == ["both.md"]


def test_a_file_or_directory_in_the_way_of_a_library_path_is_a_conflict(store):
    store.snapshots[store.head] = store.snapshot({"notes/plan.md": b"base", "page.md": b"base"})
    seed_files(store, store.connection, "worker")
    root = store.workspace("worker")
    (root / "notes/plan.md").unlink()
    (root / "notes").rmdir()
    (root / "notes").write_bytes(b"a file where the Library has a folder")
    (root / "page.md").unlink()
    (root / "page.md").mkdir()
    store.head = "2" * 40
    store.snapshots[store.head] = store.snapshot({"notes/plan.md": b"new", "page.md": b"new"})
    report = seed_files(store, store.connection, "worker")
    assert {item["path"]: item["reason"] for item in report["conflicts"]} == {
        "notes/plan.md": "path_obstructed", "page.md": "path_obstructed"}
    assert (root / "notes").read_bytes() == b"a file where the Library has a folder"


def test_a_refresh_still_refuses_links_and_case_aliases(store):
    store.snapshots[store.head] = store.snapshot({"Lib/a.md": b"base"})
    seed_files(store, store.connection, "worker")
    root = store.workspace("worker")
    store.head = "2" * 40
    store.snapshots[store.head] = store.snapshot({"Lib/a.md": b"new"})
    (root / "lib").mkdir()  # Differs from Lib only in case.
    with pytest.raises(ContentError, match="case aliases"):
        seed_files(store, store.connection, "worker")
    (root / "lib").rmdir()
    (root / "Lib/a.md").unlink()
    (root / "Lib/a.md").symlink_to(root / ".private")
    with pytest.raises(ContentError, match="Linked"):
        seed_files(store, store.connection, "worker")

