from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import pytest

from formal_geyser.coordination import content as content_module
from formal_geyser.coordination.content import (
    PRIVATE_DIRECTORY,
    ContentConflict,
    ContentError,
    FileReference,
    NativeContentStore,
    verify_references,
)
from formal_geyser.coordination.repository import RepositoryStore
from formal_geyser.coordination.storage import BlobStore, digest


def snapshot(blobs: BlobStore, files: dict[str, bytes]) -> str:
    return blobs.put_json({"files": [
        {"path": name, "sha256": blobs.put(body)} for name, body in sorted(files.items())
    ]})


@pytest.fixture
def content(tmp_path):
    return NativeContentStore(tmp_path / ".task-based")


def replace(root, path, body, expected_sha256=None):
    """The compare-and-swap replacement behind the Library and draft pointers."""
    NativeContentStore._replace_file(root, path, body, expected_sha256)


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path, body: bytes, commit=None) -> FileReference:
    return FileReference(path, hashlib.sha256(body).hexdigest(), len(body), commit)


def test_task_files_are_immutable_authority_and_preserve_exact_markdown(content):
    contract = {"title": "Native task", "objective": "# Goal\r\n\nExact bytes: α\n",
                "context": "Read the pinned wiki.\r\n", "payload": {"nested": [1, True]}}
    view = content.task_files("task-1", 2, contract)
    assert view.contract_sha256 == digest(contract)
    assert view.path.name == "task.json"
    assert view.root.is_relative_to(content.project_root / "tasks" / "task-1" / "revisions")
    assert (view.root / "objective.md").read_bytes() == contract["objective"].encode("utf-8")
    task = json.loads(view.path.read_bytes())
    assert task["objective"] == "objective.md"
    assert "objective" not in task["contract"]
    assert content.read_task("task-1", 2, view.contract_sha256) == contract
    assert content.task_files("task-1", 2, contract) == view
    assert {item.path for item in view.files} == {"task.json", "objective.md", "context.md"}
    verify_references(view.root, view.files)


def test_rollback_orphans_cannot_overwrite_same_task_revision(content):
    first = content.task_files("task", 1, {"objective": "First", "context": ""})
    second = content.task_files("task", 1, {"objective": "Second", "context": ""})
    assert first.root != second.root
    assert content.read_task("task", 1, first.contract_sha256)["objective"] == "First"
    assert content.read_task("task", 1, second.contract_sha256)["objective"] == "Second"
    assert first.as_dict()["path"] == str(first.path)


def test_read_task_does_not_create_or_touch_the_installation_lock(content):
    contract = {"objective": "Read-only monitoring", "context": ""}
    view = content.task_files("task", 1, contract)
    lock = content.root / "lock"
    lock.unlink()
    assert content.read_task("task", 1, view.contract_sha256) == contract
    assert not lock.exists()


def test_task_file_tampering_is_not_repaired_from_supplied_contract(content):
    contract = {"objective": "Authoritative objective", "context": ""}
    view = content.task_files("task", 1, contract)
    objective = view.root / "objective.md"
    objective.chmod(stat.S_IREAD | stat.S_IWRITE)
    objective.write_bytes(b"outside edit")
    with pytest.raises(ContentError, match="integrity"):
        content.read_task("task", 1, view.contract_sha256)
    with pytest.raises(ContentError, match="integrity"):
        content.task_files("task", 1, contract)
    assert objective.read_bytes() == b"outside edit"


def test_task_reference_hash_cannot_be_forged_by_rewriting_manifest(content):
    view = content.task_files("task", 1, {"objective": "Original", "context": ""})
    objective = view.root / "objective.md"
    objective.chmod(stat.S_IREAD | stat.S_IWRITE)
    objective.write_bytes(b"Forged")
    manifest_path = view.root.parent / "manifest.json"
    manifest_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    manifest = json.loads(manifest_path.read_bytes())
    for item in manifest["files"]:
        if item["path"] == "objective.md":
            item.update(sha256=hashlib.sha256(b"Forged").hexdigest(), bytes=6)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ContentError, match="task integrity"):
        content.read_task("task", 1, view.contract_sha256)


def test_private_notes_have_their_own_directory_in_the_worker_workspace(content):
    root = content.worker_notes("worker")
    source = content.state_dir / "workers" / "worker" / "work"
    assert root == source / PRIVATE_DIRECTORY / "notes"
    assert "wiki" not in root.parts
    with pytest.raises(ContentError, match="owned worker"):
        content.worker_notes("worker", source.parent)


def test_replacing_a_native_file_requires_compare_and_swap(content):
    root = content.worker_notes("worker")
    replace(root, "research/observations.md", "Draft\r\nα".encode())
    assert (root / "research/observations.md").read_bytes() == "Draft\r\nα".encode()
    first = sha(root / "research/observations.md")
    with pytest.raises(ContentConflict):
        replace(root, "research/observations.md", b"Accidental overwrite")
    with pytest.raises(ContentConflict):
        replace(root, "research/observations.md", b"Stale", "0" * 64)
    replace(root, "research/observations.md", b"Revised", first)
    assert (root / "research/observations.md").read_bytes() == b"Revised"
    with pytest.raises(ContentConflict, match="missing"):
        replace(root, "missing.md", b"No source", first)


def test_concurrent_creates_never_replace_another_file(content):
    root = content.worker_notes("worker")

    def create(number):
        independent = NativeContentStore(content.state_dir)
        try:
            with independent._locked():
                replace(root, "shared.md", str(number).encode())
            return number
        except ContentConflict:
            return None
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(create, range(12)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert (root / "shared.md").read_bytes() == str(winners[0]).encode()
    assert not list(root.glob(".native-replace-*"))


@pytest.mark.parametrize("path", [
    "../outside.md", "/absolute.md", "C:/outside.md", "nested\\file.md", "a//file.md",
    "a/./file.md", "file.md:stream", "NUL.md", "lpt².md", "CONIN$.md", "trailing./a.md",
    "trailing /a.md", "line\nbreak.md", ".git/config.md", ".gıt/config.md", "GIT~1/a.md",
    ".task-based/secret.md", ".taskhub-integration/review/file.md", ".PrIvAtE/note.md",
])
def test_native_references_and_replacements_reject_unsafe_paths(content, path):
    root = content.worker_notes("worker")
    with pytest.raises(ContentError):
        replace(root, path, b"untrusted")
    with pytest.raises(ContentError):
        verify_references(root, [reference(path, b"")])


@pytest.mark.parametrize("identifier", ["../escape", "C:", "NUL", "a/b", "a\\b", ".", ""])
def test_owner_ids_cannot_escape_layout(content, identifier):
    with pytest.raises(ContentError):
        content.worker_notes(identifier)
    with pytest.raises(ContentError):
        content.task_files(identifier, 1, {"objective": "Goal"})


def test_native_paths_reject_case_aliases(content):
    root = content.worker_notes("worker")
    replace(root, "Research/note.md", b"original")
    with pytest.raises(ContentError, match="case"):
        replace(root, "research/other.md", b"alias")
    with pytest.raises(ContentError, match="case"):
        content.worker_notes("WORKER")


def _settled_folders(tmp_path, *names):
    folders = []
    for name in names:
        folder = tmp_path / name
        (folder / "entry").mkdir(parents=True)
        os.utime(folder, ns=(0, 0))  # Unchanged for long enough that its listing is trusted.
        folders.append(folder)
    return folders


def _counted_listings(monkeypatch, delay=0.0):
    listed, original = [], os.scandir

    def scandir(path):
        listed.append(str(path))
        time.sleep(delay)
        return original(path)
    monkeypatch.setattr(content_module, "_LISTINGS", OrderedDict())
    monkeypatch.setattr(content_module.os, "scandir", scandir)
    return listed


def test_a_folder_listed_often_stays_cached_when_the_listings_overflow(tmp_path, monkeypatch):
    hot, *others = _settled_folders(tmp_path, "workers", "a", "b", "c")
    listed = _counted_listings(monkeypatch)
    monkeypatch.setattr(content_module, "_MAX_LISTINGS", 2)
    for folder in others:
        assert content_module._same_folded_names(hot, "entry") == ["entry"]
        content_module._same_folded_names(folder, "entry")
    # The least recently used listings left; the one used every time was listed once.
    assert listed.count(str(hot)) == 1 and len(content_module._LISTINGS) == 2


def test_threads_checking_one_folder_together_share_one_listing(tmp_path, monkeypatch):
    [folder] = _settled_folders(tmp_path, "workers")
    listed = _counted_listings(monkeypatch, delay=0.2)
    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda _: content_module._same_folded_names(folder, "entry"), range(8)))
    assert found == [["entry"]] * 8 and listed == [str(folder)]


def test_a_case_alias_made_just_after_a_listing_is_still_refused(content):
    root = content.worker_notes("worker")
    replace(root, "Research/note.md", b"original")
    (root / "research").mkdir()  # The folder changed within the last two seconds.
    with pytest.raises(ContentError, match="case"):
        replace(root, "Research/second.md", b"second")


def test_private_directory_case_alias_is_not_silently_adopted(content):
    worker = content.state_dir / "workers" / "worker" / "work"
    (worker / ".PRIVATE" / "notes").mkdir(parents=True)
    with pytest.raises(ContentError, match="case"):
        content.worker_notes("worker")


def test_native_reference_list_rejects_case_and_file_directory_collisions(tmp_path):
    for paths in (["A.md", "a.md"], ["Dir/a.md", "dir/b.md"], ["a", "a/b"], ["a", "a"]):
        with pytest.raises(ContentError):
            verify_references(tmp_path, [reference(path, b"") for path in paths])


def test_linked_roots_and_files_are_not_native_content(content, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("secret")
    root = content.worker_notes("worker")
    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires platform privileges")
    with pytest.raises(ContentError, match="Linked"):
        replace(root, "linked/note.md", b"overwrite")
    with pytest.raises(ContentError, match="Linked"):
        verify_references(root, [reference("linked/note.md", b"secret")])
    with pytest.raises(ContentError, match="Linked"):
        NativeContentStore(linked / "state")
    assert (outside / "note.md").read_text() == "secret"


def test_hard_link_notes_are_rejected(content, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("unrelated")
    root = content.worker_notes("worker")
    try:
        os.link(outside, root / "linked.md")
    except OSError:
        pytest.skip("This filesystem does not support hard links")
    with pytest.raises(ContentError, match="hard links"):
        verify_references(root, [reference("linked.md", b"unrelated")])
    with pytest.raises(ContentError, match="hard links"):
        replace(root, "linked.md", b"overwrite", "0" * 64)
    assert outside.read_text() == "unrelated"


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_public_view_is_exact_git_source_without_library_relocation(content, tmp_path):
    blobs = BlobStore(content.state_dir / "objects")
    repository = RepositoryStore(content.state_dir)
    original = {"Math/Basic.lean": b"def claim : Prop := True\r\n",
                "Math/Basic.md": "# Claim α\n".encode(),
                "wiki/Start.md": b"Read this first.\r\n",
                "lakefile.lean": b"-- build configuration\n", "data.bin": b"\x00\xff"}
    commit = repository.initialize(blobs, snapshot(blobs, original))["head"]
    view = content.public_view(repository, blobs, commit)
    assert view.root == tmp_path / "library" / commit / "files"
    assert {item.path: (view.root / item.path).read_bytes() for item in view.files} == original
    assert [item.path for item in view.wiki] == ["wiki/Start.md"]
    assert all(item.commit == commit for item in view.files)
    assert repository.head() == commit
    assert content.public_view(repository, blobs, commit) == view
    assert {item["commit"] for item in view.as_dict()["files"]} == {commit}
    pointer = content.publish_current_view(view)
    assert json.loads(pointer.read_bytes())["commit"] == commit
    assert content.publish_current_view(view) == pointer
    assert view.knowledge == view.wiki
    target = view.root / "wiki" / "Start.md"
    with pytest.raises(PermissionError):
        target.write_bytes(b"accidental edit")
    target.chmod(stat.S_IREAD | stat.S_IWRITE)
    target.write_bytes(b"tampered")
    with pytest.raises(ContentError, match="integrity"):
        content.public_view(repository, blobs, commit)


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_public_knowledge_keeps_exact_files_across_publications(content, tmp_path):
    blobs = BlobStore(content.state_dir / "objects")
    repository = RepositoryStore(content.state_dir)
    initial = {"wiki/Unchanged.md": b"Same", "wiki/Changed.md": b"Before",
               "wiki/Removed.md": b"Removed", "wiki/image.png": b"asset", "outside.md": b"old"}
    commit = repository.initialize(blobs, snapshot(blobs, initial))["head"]
    before = content.public_view(repository, blobs, commit)
    content.publish_current_view(before)
    final = {"wiki/Unchanged.md": b"Same", "wiki/Changed.md": b"After", "wiki/New.md": b"New",
             "wiki/image.png": b"asset", "outside.md": b"new"}
    work = tmp_path / "review" / ".taskhub-integration" / "change"
    repository.prepare(blobs, snapshot(blobs, final), commit, work, sorted(set(initial) | set(final)))
    frozen = repository.finalize(work, blobs)
    repository.publish(frozen["commit"], commit)
    after = content.public_view(repository, blobs, frozen["commit"])
    assert {item.path for item in after.knowledge} == {"wiki/Changed.md", "wiki/New.md", "wiki/Unchanged.md"}
    assert (before.root / "wiki/Changed.md").read_bytes() == b"Before"
    assert (after.root / "wiki/Changed.md").read_bytes() == b"After"
    with pytest.raises(ContentConflict, match="current Library commit"):
        content.publish_current_view(after)
    with pytest.raises(ContentConflict, match="changed"):
        content.publish_current_view(after, expected_commit="0" * 40)
    pointer = content.publish_current_view(after, expected_commit=before.commit)
    assert json.loads(pointer.read_bytes())["commit"] == after.commit
    pointer.write_bytes(b"unknown human content")
    with pytest.raises(ContentConflict, match="Invalid current"):
        content.publish_current_view(before, expected_commit=after.commit)
    assert pointer.read_bytes() == b"unknown human content"


def test_unexpected_file_is_not_silently_included_in_immutable_view(content):
    contract = {"objective": "Task", "context": ""}
    view = content.task_files("task", 1, contract)
    (view.root / "extra.md").write_text("unindexed")
    with pytest.raises(ContentError, match="tree differs"):
        content.task_files("task", 1, contract)


def test_file_reference_roundtrip_is_hash_bound_and_frozen(tmp_path):
    (tmp_path / "file.md").write_bytes(b"body")
    refs = [reference("file.md", b"body")]
    assert FileReference.from_dict(refs[0].as_dict()) == refs[0]
    verify_references(tmp_path, refs)
    (tmp_path / "file.md").write_bytes(b"changed")
    with pytest.raises(ContentError, match="integrity"):
        verify_references(tmp_path, refs)


def test_atomic_replacement_failure_preserves_original_and_cleans_temporary(content, monkeypatch):
    root = content.worker_notes("worker")
    replace(root, "note.md", b"Original")
    first = sha(root / "note.md")
    def fail_replace(*_args):
        raise OSError("synthetic replacement failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic"):
        replace(root, "note.md", b"After", first)
    assert (root / "note.md").read_text() == "Original"
    assert not list(root.glob(".native-replace-*"))


def test_atomic_task_install_failure_does_not_leave_partial_revision(content, monkeypatch):
    def fail_rename(*_args):
        raise OSError("synthetic install failure")
    monkeypatch.setattr(os, "rename", fail_rename)
    with pytest.raises(OSError, match="synthetic"):
        content.task_files("task", 1, {"objective": "Goal", "context": ""})
    assert list((content.tasks_root / "task" / "revisions").iterdir()) == []


def test_empty_public_tree_still_requires_its_materialized_directory(content):
    class EmptyRepository:
        def snapshot(self, _commit, blobs):
            return snapshot(blobs, {})
    blobs = BlobStore(content.state_dir / "objects")
    repository = EmptyRepository()
    view = content.public_view(repository, blobs, "a" * 40)
    assert view.files == ()
    view.root.rmdir()
    with pytest.raises(ContentError, match="tree is missing"):
        content.public_view(repository, blobs, "a" * 40)


def test_public_native_view_rejects_private_content_even_if_other_ingress_missed_it(content):
    class PrivateRepository:
        def snapshot(self, _commit, blobs):
            return snapshot(blobs, {".PRIVATE/notes/research.md": b"private"})
    blobs = BlobStore(content.state_dir / "objects")
    with pytest.raises(ContentError, match="Unsafe"):
        content.public_view(PrivateRepository(), blobs, "a" * 40)
    assert not content.library_root.exists()
