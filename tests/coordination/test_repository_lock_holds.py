"""Worker checkouts do not wait behind long repository work that needs no lock.

In the Linux pilot the Integrator pruned an old integration checkout under the repository
lock: its walk and snapshot took over 30 seconds while other threads kept the interpreter
busy, and a worker checkout waiting for the lock failed after its claim.
"""
import os
import shutil
import time

import pytest

from formal_geyser.coordination import repository as repository_module
from formal_geyser.coordination.repository import RepositoryBusy, RepositoryStore
from formal_geyser.coordination.storage import BlobStore
from tests.coordination.test_repository import freeze, manifest, workspace
from tests.coordination.test_repository import store as store  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


def lock_is_free(repository):
    # Another store, as each Hub call makes: it meets a holder only at the file lock.
    with RepositoryStore(repository.state_dir, branch=repository.branch)._locked():
        return True


def settle(repository):
    old = time.time() - 3600
    for path in (repository.control / "owner.json", repository.control / "empty-hooks",
                 repository.control / "empty-attributes", repository.path, repository.path / "config",
                 repository.path / "HEAD", repository.path / "objects" / "info", repository.path / "refs"):
        os.utime(path, (old, old))


def test_a_held_lock_fails_with_a_named_retryable_error(store, monkeypatch):
    repository, blobs = store
    repository.initialize(blobs)
    monkeypatch.setattr(repository_module, "LOCK_SECONDS", 0.2)
    with RepositoryStore(repository.state_dir)._locked():
        with pytest.raises(RepositoryBusy, match="stayed locked by another operation for 0.2 seconds"):
            lock_is_free(repository)
    assert isinstance(RepositoryBusy("x"), OSError)  # Callers that retry OS failures retry it.
    assert lock_is_free(repository)


def test_preparing_and_freezing_read_the_checkout_without_the_repository_lock(store, tmp_path, monkeypatch):
    repository, blobs = store
    base = repository.initialize(blobs, manifest(blobs, {"Old.lean": b"def y := 0", "Old.md": b"Zero."}))["head"]
    monkeypatch.setattr(repository_module, "LOCK_SECONDS", 0.2)
    observed = []
    # The walks of the checkout and the reads of immutable objects: seconds each on a busy
    # interpreter, for a Library of a thousand files.
    for name in ("_entries", "_complete_prepare", "_check_partial_checkout", "_working_bytes", "_snapshot"):
        def checked(self, *args, _name=name, _original=getattr(RepositoryStore, name), **kwargs):
            observed.append((_name, lock_is_free(repository)))
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(RepositoryStore, name, checked)
    final = freeze(store, tmp_path, "batch", {"Example.lean": b"def x := 1", "Example.md": b"One."}, base)
    monkeypatch.undo()
    assert {name for name, _ in observed} == {
        "_entries", "_complete_prepare", "_check_partial_checkout", "_working_bytes", "_snapshot"}
    assert all(free for _, free in observed)
    assert repository.snapshot(final["commit"], blobs) == final["snapshot"]
    assert {item["path"] for item in final["files"]} == {"Old.lean", "Old.md", "Example.lean", "Example.md"}


def test_a_checkout_waits_only_for_its_own_operations(store, tmp_path, monkeypatch):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    freeze(store, tmp_path, "batch", {"Work.md": b"first"}, base)
    location = workspace(tmp_path, "batch")
    monkeypatch.setattr(repository_module, "LOCK_SECONDS", 0.2)
    with RepositoryStore(repository.state_dir)._session_locked(repository._session_path(location)[1]):
        with pytest.raises(RepositoryBusy, match="integration checkout stayed locked"):
            repository.status(location)
        with pytest.raises(RepositoryBusy, match="integration checkout stayed locked"):
            repository.prune_integration_workspace(location, blobs)
        assert lock_is_free(repository)  # Worker checkouts and publication do not wait.
        other = freeze(store, tmp_path, "other", {"Other.md": b"second"}, base)  # Nor another batch.
    assert other["commit"] and repository.status(location)["finalized"]


def test_new_objects_enter_the_shared_repository_under_its_lock(store, tmp_path, monkeypatch):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    location = workspace(tmp_path, "batch")
    monkeypatch.setattr(repository_module, "LOCK_SECONDS", 0.2)
    with RepositoryStore(repository.state_dir)._locked():
        with pytest.raises(RepositoryBusy, match="Library repository stayed locked"):
            repository.prepare(blobs, manifest(blobs, {"Work.md": b"first"}), base, location, ["Work.md"])
    assert not location.exists() and not repository._session_path(location)[1].exists()
    assert freeze(store, tmp_path, "batch", {"Work.md": b"first"}, base)["commit"]  # A retry starts afresh.


def test_pruning_reads_and_removes_the_checkout_without_the_repository_lock(store, tmp_path, monkeypatch):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    final = freeze(store, tmp_path, "finalized", {"Example.lean": b"def x := 1", "Example.md": b"One."}, base)
    location = workspace(tmp_path, "finalized")
    monkeypatch.setattr(repository_module, "LOCK_SECONDS", 0.2)
    observed = []
    snapshot, rmtree = BlobStore.snapshot, shutil.rmtree

    def checked_snapshot(self, directory, **kwargs):
        observed.append(("snapshot", lock_is_free(repository)))
        return snapshot(self, directory, **kwargs)

    def checked_rmtree(path, *args, **kwargs):
        observed.append(("rmtree", lock_is_free(repository)))
        return rmtree(path, *args, **kwargs)
    monkeypatch.setattr(BlobStore, "snapshot", checked_snapshot)
    monkeypatch.setattr(shutil, "rmtree", checked_rmtree)
    result = repository.prune_integration_workspace(location, blobs)
    monkeypatch.undo()
    assert result["state"] == "reclaimed" and not location.exists()
    assert observed == [("snapshot", True), ("rmtree", True)]
    assert blobs.get_json(final["snapshot"])["files"]


def test_an_absent_checkout_is_skipped_without_the_lock(store, tmp_path, monkeypatch):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    freeze(store, tmp_path, "finalized", {"Work.md": b"first"}, base)
    location = workspace(tmp_path, "finalized")
    assert repository.prune_integration_workspace(location, blobs)["state"] == "reclaimed"
    monkeypatch.setattr(RepositoryStore, "_locked", lambda self: pytest.fail("Nothing to remove needs no lock"))
    assert repository.prune_integration_workspace(location, blobs)["state"] == "already_absent"


def test_a_checkout_changed_while_it_was_checked_is_kept(store, tmp_path, monkeypatch):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    freeze(store, tmp_path, "finalized", {"Work.md": b"first"}, base)
    location = workspace(tmp_path, "finalized")
    _, metadata = repository._session_path(location)
    snapshot = BlobStore.snapshot

    def concurrent_record(self, directory, **kwargs):
        result = snapshot(self, directory, **kwargs)
        session = repository._read_json(metadata)
        repository._write_json(metadata, {**session, "note": "changed meanwhile"})
        return result
    monkeypatch.setattr(BlobStore, "snapshot", concurrent_record)
    assert repository.prune_integration_workspace(location, blobs)["state"] == "retained_modified"
    assert (location / "Work.md").read_bytes() == b"first"


def test_a_published_snapshot_and_the_head_are_reused_without_the_lock(store, tmp_path, monkeypatch):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    final = freeze(store, tmp_path, "finalized", {"Example.lean": b"def x := 1"}, base)
    settle(repository)
    # Any locked operation's ownership check serves the reads below (here a first snapshot).
    assert blobs.get_json(repository.snapshot(base, blobs))["files"] == []
    monkeypatch.setattr(RepositoryStore, "_locked", lambda self: pytest.fail("A known answer takes no lock"))
    assert repository.snapshot(final["commit"], blobs) == final["snapshot"]  # Remembered when frozen.
    assert repository.snapshot(base, blobs)
    assert repository.head() == base
    monkeypatch.undo()
    (repository.control / "empty-hooks" / "post-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(repository_module.RepositoryError, match="safety configuration"):
        repository.snapshot(final["commit"], blobs)  # A changed control folder is checked again.
