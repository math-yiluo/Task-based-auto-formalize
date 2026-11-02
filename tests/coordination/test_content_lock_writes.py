"""Library views and archive moves write their files outside the content lock, flushed once, and
admission leaves archive moves to commands: after each publication in the Linux pilot these
held the lock for minutes and nobody was admitted meanwhile."""
import os
import threading

import pytest

from formal_geyser.coordination import content as content_module
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.content import NativeContentStore
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import ProjectConfig
from formal_geyser.coordination.repository import RepositoryStore
from formal_geyser.coordination.storage import BlobStore, digest
from tests.coordination.test_artifact_archive import staged as staged  # noqa: F401
from tests.coordination.test_collaborative_core import command
from tests.coordination.test_native_content import snapshot

LIBRARY = {f"Math/Part{number}.lean": f"def part{number} : Prop := True\n".encode() for number in range(6)}


@pytest.fixture
def library(tmp_path):
    content = NativeContentStore(tmp_path / ".task-based")
    blobs = BlobStore(content.state_dir / "objects")
    repository = RepositoryStore(content.state_dir)
    commit = repository.initialize(blobs, snapshot(blobs, LIBRARY))["head"]
    return content, repository, blobs, commit


def lock_free_while(call, store):
    """Run `call` in a thread, hold its first file write, and take the content lock meanwhile."""
    writing, release = threading.Event(), threading.Event()
    original = content_module.FreshTree.write

    def held(tree, name, body, **options):
        if not writing.is_set():
            writing.set()
            release.wait(30)
        return original(tree, name, body, **options)
    content_module.FreshTree.write = held  # type: ignore[method-assign]
    result, failure = [], []

    def run():
        try:
            result.append(call())
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            failure.append(error)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert writing.wait(10)
        taken = threading.Event()

        def take():
            with store._locked():
                taken.set()
        other = threading.Thread(target=take)
        other.start()
        other.join(5)
        assert taken.is_set(), "the content lock was held while files were written"
    finally:
        release.set()
        worker.join(30)
        content_module.FreshTree.write = original  # type: ignore[method-assign]
    assert not failure, failure
    return result[0]


def count_flushes(monkeypatch):
    calls = {"fsync": 0, "sync": 0}
    fsync = os.fsync

    def counted_fsync(descriptor):
        calls["fsync"] += 1
        return fsync(descriptor)

    def counted_sync():
        calls["sync"] += 1
    monkeypatch.setattr(os, "fsync", counted_fsync)
    monkeypatch.setattr(os, "sync", counted_sync, raising=False)
    return calls


def test_a_library_view_is_written_outside_the_content_lock(library):
    content, repository, blobs, commit = library
    view = lock_free_while(lambda: content.public_view(repository, blobs, commit), content)
    assert {item.path: (view.root / item.path).read_bytes() for item in view.files} == LIBRARY
    assert content.public_view(repository, blobs, commit) == view


def test_a_library_view_is_flushed_once_not_file_by_file(library, monkeypatch):
    content, repository, blobs, commit = library
    calls = count_flushes(monkeypatch)
    content.public_view(repository, blobs, commit)
    assert calls == {"fsync": 0, "sync": 1}


def test_an_archive_move_copies_outside_the_content_lock(staged):  # noqa: F811
    store, data, sources, path = staged
    receipt = {"disposition": "published", "commit": "a" * 40}
    archived = lock_free_while(
        lambda: store.relocate(path.parent, "candidate", data, "artifacts", receipt), store.content)
    assert archived == store.root / "artifacts/candidate/2/record.json"
    store.verify(archived.parent, "candidate", 2, digest(data))
    assert store.resolve(path, "candidate", digest(data)) == archived
    assert not list((store.root / "artifacts/candidate").glob(".native-content-*"))


def test_an_archive_move_flushes_its_copy_once(staged, monkeypatch):  # noqa: F811
    store, data, _, path = staged
    calls = count_flushes(monkeypatch)
    store.relocate(path.parent, "candidate", data, "artifacts", {"disposition": "published", "commit": "a" * 40})
    # One flush for the copy; the journal and the old folder's marker keep their own fsync.
    assert calls == {"fsync": 2, "sync": 1}


async def test_admission_and_driver_recovery_leave_archive_moves_to_commands(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        run = await command(hub, "run.start")
        moves = []

        async def reconcile():
            moves.append(True)
            hub._archive_relocations_pending = False
            return []
        monkeypatch.setattr(hub, "reconcile_archive_relocations", reconcile)
        hub._archive_relocations_pending = True
        await hub.next_assignment(run["id"])
        await hub.recover_quiet_runtime_failures(run["id"])
        assert moves == []
        await command(hub, "worker.register")
        await hub.archive_moves_settled()  # A command has them made in the background.
        assert moves == [True]
    finally:
        await hub.close()
