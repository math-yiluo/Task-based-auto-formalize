"""Immutable content is verified when it enters, moves or is used; not again on every read."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from uuid import uuid4

import pytest

from formal_geyser.coordination import content as content_module
from formal_geyser.coordination import literature as literature_module
from formal_geyser.coordination import native as native_module
from formal_geyser.coordination import repository as repository_module
from formal_geyser.coordination.artifact_files import ArtifactFiles
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.content import NativeContentStore, _path
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.repository import RepositoryStore
from formal_geyser.coordination.storage import BlobStore
from tests.coordination.test_review_entry import review_case  # noqa: F401 (fixture)


def settle(*paths: Path) -> None:
    """Age files and folders past the settling window, as if written long ago."""
    old = time.time() - 3600
    for path in paths:
        os.utime(path, (old, old))


def settle_tree(root: Path) -> None:
    """Age a folder and everything in it."""
    settle(root, *(Path(current) / name for current, dirs, files in os.walk(root)
                   for name in (*dirs, *files)))


def test_blob_bytes_are_reused_until_the_stored_file_changes(tmp_path, monkeypatch):
    blobs = BlobStore(tmp_path / "objects")
    sha = blobs.put(b"immutable body")
    stored = blobs.root / sha[:2] / sha
    reads = []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or original(self))
    assert blobs.get(sha) == blobs.get(sha) == b"immutable body"
    assert len(reads) == 2  # A blob written moments ago is read again until it settles.
    settle(stored)
    assert blobs.get(sha) == blobs.get(sha) == b"immutable body"
    assert len(reads) == 3  # Verified once more, then reused while the file is unchanged.
    stored.chmod(stat.S_IREAD | stat.S_IWRITE)
    stored.write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        blobs.get(sha)


async def test_pinned_archive_reads_are_reused_across_calls_until_a_checked_file_changes(review_case, monkeypatch):  # noqa: F811
    case = await review_case(kind="lean.definition")
    root = Path((await case.hub.artifact(case.candidate["id"]))["content_file"]["root"])
    settle_tree(root)
    reads = []
    original = native_module._read
    monkeypatch.setattr(native_module, "_read", lambda path: reads.append(Path(path).name) or original(path))
    assert (await case.hub.artifact(case.candidate["id"]))["data"] == case.data
    assert (await case.hub.artifact(case.candidate["id"]))["data"] == case.data
    assert reads.count("record.json") == reads.count("Fixture.lean") == 1  # Checked once, then reused.
    pair = root / "files/Fixture.lean"
    pair.chmod(stat.S_IREAD | stat.S_IWRITE)
    pair.write_bytes(b"Changed frozen pair")
    with pytest.raises(Exception):
        await case.hub.artifact(case.candidate["id"])


async def test_record_reads_check_the_pinned_manifest_and_pair_not_every_archived_file(review_case, monkeypatch):  # noqa: F811
    case = await review_case(kind="lean.definition")
    passes = []
    original = ArtifactFiles.verify

    def counted(self, *args, **kwargs):
        passes.append(args[0])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ArtifactFiles, "verify", counted)
    record = await case.hub.artifact(case.candidate["id"])
    assert record["data"] == case.data and passes == []
    # The Lean/Markdown pair is still checked on every read.
    pair = Path(record["content_file"]["root"]) / "files/Fixture.lean"
    pair.chmod(stat.S_IREAD | stat.S_IWRITE)
    pair.write_bytes(b"Changed frozen pair")
    with pytest.raises(Exception):
        await case.hub.artifact(case.candidate["id"])


async def test_task_revisions_are_verified_again_only_after_a_change(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        task = await hub.execute(Command(name="task.publish", key=uuid4().hex, payload={
            "contract": {"title": "Cached task", "objective": "Read me once."}}), Actor(role=Role.OPERATOR))
        root = Path((await hub.task(task["id"]))["files"]["root"])
        revision = root.parent
        settle(revision, revision / "manifest.json", root,
               *(root / name for name in ("context.md", "objective.md", "task.json")))
        trees = []
        original = NativeContentStore._verify_tree
        monkeypatch.setattr(NativeContentStore, "_verify_tree",
                            staticmethod(lambda folder, refs: trees.append(folder) or original(folder, refs)))
        assert (await hub.task(task["id"]))["contract"]["objective"] == "Read me once."
        assert (await hub.task(task["id"]))["contract"]["objective"] == "Read me once."
        assert len(trees) == 1  # Verified once; the second read compared file identities.
        target = root / "objective.md"
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
        target.write_bytes(b"Unexpected external edit.\n")
        with pytest.raises(Exception):
            await hub.task(task["id"])
    finally:
        await hub.close()


def test_case_alias_listings_are_rescanned_only_when_a_folder_changes(tmp_path, monkeypatch):
    folder = tmp_path / "root" / "items"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("fixture", encoding="utf-8")
    settle(folder / "a.txt", folder, folder.parent)
    scans = []
    original = os.scandir

    def counted(path):
        scans.append(os.path.normcase(os.fspath(path)))
        return original(path)
    monkeypatch.setattr(content_module.os, "scandir", counted)
    for _ in range(3):
        assert _path(tmp_path / "root", "items/a.txt") == folder / "a.txt"
    listed = [path for path in scans if path.endswith(os.path.normcase("items"))]
    assert len(listed) == 1
    (folder / "b.txt").write_text("new entry", encoding="utf-8")  # The folder changed.
    _path(tmp_path / "root", "items/a.txt")
    assert len([path for path in scans if path.endswith(os.path.normcase("items"))]) == 2


def test_literature_digests_are_reused_until_the_file_changes(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fixture " * 64)
    settle(source)
    digests = []
    original = literature_module.hashlib.file_digest
    monkeypatch.setattr(literature_module.hashlib, "file_digest",
                        lambda stream, name: digests.append(name) or original(stream, name))
    first = literature_module.fingerprint(source)
    assert literature_module.fingerprint(source) == first and len(digests) == 1
    source.write_bytes(b"%PDF other edition " * 64)
    assert literature_module.fingerprint(source) != first and len(digests) == 2


def test_repository_ownership_walk_repeats_on_control_changes_and_each_minute(tmp_path, monkeypatch):
    repository = RepositoryStore(tmp_path / "state")
    repository.initialize(BlobStore(tmp_path / "state" / "blobs"))
    walks = []
    original = RepositoryStore._owned
    monkeypatch.setattr(RepositoryStore, "_owned", lambda self: walks.append(1) or original(self))
    first = repository.head()
    assert len(walks) == 1  # Freshly written control files are always checked in full.
    control = [repository.control / "owner.json", repository.control / "empty-hooks",
               repository.control / "empty-attributes", repository.path, repository.path / "config",
               repository.path / "HEAD", repository.path / "objects" / "info", repository.path / "refs"]
    settle(*control)
    repository.head()
    assert repository.head() == first and len(walks) == 2  # Checked once, then reused.
    monkeypatch.setattr(repository_module, "_OWNERSHIP_SECONDS", 0.0)
    repository.head()
    assert len(walks) == 3  # The full check repeats at least once a minute.
    monkeypatch.setattr(repository_module, "_OWNERSHIP_SECONDS", 60.0)
    hook = repository.control / "empty-hooks" / "post-commit"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(repository_module.RepositoryError, match="safety configuration"):
        repository.head()


@pytest.mark.skipif(__import__("shutil").which("git") is None, reason="Native Git is required")
def test_verified_library_views_are_reused_until_a_view_file_changes(tmp_path, monkeypatch):
    from formal_geyser.coordination.content import ContentError
    from tests.coordination.test_native_content import snapshot

    content = NativeContentStore(tmp_path / ".task-based")
    blobs = BlobStore(content.state_dir / "objects")
    repository = RepositoryStore(content.state_dir)
    files = {"Math/Basic.lean": b"def claim : Prop := True\n", "Math/Basic.md": b"# Claim\n",
             "notes/start.md": b"Read this first.\n"}
    commit = repository.initialize(blobs, snapshot(blobs, files))["head"]
    view = content.public_view(repository, blobs, commit)
    root = view.root
    settle(root.parent / "manifest.json", *(root / item.path for item in view.files),
           root / "Math", root / "notes", root)
    assert content.public_view(repository, blobs, commit) == view  # Verified and remembered.
    reads = []
    original = BlobStore.get
    monkeypatch.setattr(BlobStore, "get", lambda self, sha: reads.append(sha) or original(self, sha))
    monkeypatch.setattr(NativeContentStore, "_locked", lambda self: pytest.fail("A reused view takes no lock"))
    assert content.public_view(repository, blobs, commit) == view
    assert reads == []
    monkeypatch.undo()
    target = root / "notes/start.md"
    target.chmod(stat.S_IREAD | stat.S_IWRITE)
    target.write_bytes(b"tampered")
    with pytest.raises(ContentError, match="integrity"):
        content.public_view(repository, blobs, commit)
