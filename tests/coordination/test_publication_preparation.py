"""Git checks for a publication run before its transaction; the coordination thread never walks the repository periodically."""
import os
import shutil
import threading
import time

import pytest

from formal_geyser.coordination import repository as repository_module
from formal_geyser.coordination import storage
from formal_geyser.coordination.repository import RepositoryStore
from formal_geyser.coordination.storage import BlobStore
from tests.coordination.publication_helpers import approve
from tests.coordination.test_archive_publication import archive_case as archive_case  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


def settle(repository):
    old = time.time() - 3600
    for path in (repository.control / "owner.json", repository.control / "empty-hooks",
                 repository.control / "empty-attributes", repository.path, repository.path / "config",
                 repository.path / "HEAD", repository.path / "objects" / "info", repository.path / "refs"):
        os.utime(path, (old, old))


def test_the_coordination_thread_reuses_an_unchanged_verification_without_the_lock(tmp_path, monkeypatch):
    repository = RepositoryStore(tmp_path / "state")
    head = repository.initialize(BlobStore(tmp_path / "state" / "blobs"))["head"]
    settle(repository)
    walks = []
    original = RepositoryStore._owned
    monkeypatch.setattr(RepositoryStore, "_owned", lambda self: walks.append(1) or original(self))
    assert repository.head() == head and len(walks) == 1
    monkeypatch.setattr(repository_module, "_OWNERSHIP_SECONDS", 0.0)
    monkeypatch.setattr(RepositoryStore, "_locked", lambda self: pytest.fail("A verified head read takes no lock"))
    storage._COORDINATION.active = True
    try:
        assert repository.head() == head and len(walks) == 1  # Reused on the coordination thread.
    finally:
        storage._COORDINATION.active = False
    monkeypatch.undo()
    monkeypatch.setattr(RepositoryStore, "_owned", lambda self: walks.append(1) or original(self))
    monkeypatch.setattr(repository_module, "_OWNERSHIP_SECONDS", 0.0)
    assert repository.head() == head and len(walks) == 2  # Other threads repeat the walk when due.
    (repository.control / "empty-hooks" / "post-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    storage._COORDINATION.active = True
    try:
        with pytest.raises(repository_module.RepositoryError, match="safety configuration"):
            repository.head()  # A changed control folder is checked even here.
    finally:
        storage._COORDINATION.active = False


async def test_a_batch_publication_transaction_only_moves_the_branch(archive_case, monkeypatch):
    case = archive_case
    calls = []
    original = RepositoryStore._git

    def observed(self, arguments, *args, **kwargs):
        calls.append((threading.current_thread().name.startswith("taskhub-db"), arguments[0]))
        return original(self, arguments, *args, **kwargs)
    monkeypatch.setattr(RepositoryStore, "_git", observed)
    walks = []
    owned = RepositoryStore._owned

    def counted(self):
        walks.append(threading.current_thread().name.startswith("taskhub-db"))
        return owned(self)
    monkeypatch.setattr(RepositoryStore, "_owned", counted)
    # A repository changed in the last two seconds is checked again in the transaction.
    settle(case.hub._repository())
    await approve(case.hub, case.reviewer)
    published = await case.hub.db.read(lambda c: c.execute(
        "SELECT commit_sha FROM repository_publications").fetchone()[0])
    assert case.hub._repository().head() == published
    in_transaction = [name for on_thread, name in calls if on_thread]
    assert "update-ref" in in_transaction and "cat-file" not in in_transaction
    assert any(name == "cat-file" for on_thread, name in calls if not on_thread)
