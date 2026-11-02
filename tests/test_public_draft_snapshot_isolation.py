"""Shared drafts remain readable files, never implicit formal source inputs."""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.integration import RepositoryCoordination
from formal_geyser.coordination.repository import RepositoryError, RepositoryStore
from formal_geyser.coordination.snapshot_limits import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
)
from formal_geyser.coordination.storage import BlobStore
from formal_geyser.lean_service.executor import (
    snapshot_sources as lean_snapshot_sources,
)
from formal_geyser.registry import (
    RegistryError,
    read_snapshot,
    snapshot_sources,
)

READY = {"Math/Ready.lean": b"def ready : Prop := True\n",
         "Math/Ready.md": b"A proposition definition ready for ordinary review.\n"}
DRAFT_MARKER = b"unresolved-draft-only-marker"


def project(root: Path):
    state = root / ".task-based"
    return SimpleNamespace(root=root, state_dir=state, blobs=BlobStore(state / "objects" / "sha256"))


def write_files(root: Path, files: dict[str, bytes]) -> None:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


def capture(hub, root: Path, mode: str, **limits) -> str:
    return (hub.blobs.snapshot(root, **limits) if mode == "core"
            else snapshot_sources(hub, root, **limits))


def manifest(blobs, files: dict[str, bytes]) -> str:
    return blobs.put_json({"files": [{"path": path, "sha256": blobs.put(body)}
                                    for path, body in sorted(files.items())]})


@pytest.mark.parametrize("mode", ["core", "domain"])
def test_workspace_snapshots_pass_over_empty_sandbox_placeholders(tmp_path, mode):
    # Claude Code's sandbox puts these empty files at a workspace's top while a command runs; one
    # cut off by a crash left them, and staging refused the hidden paths.
    hub = project(tmp_path)
    workspace = tmp_path / "work"
    write_files(workspace, {**READY, ".gitconfig": b"", ".bashrc": b"", ".vscode": b"", ".mcp.json": b"{}"})
    paths = {item["path"] for item in read_snapshot(hub.blobs, capture(hub, workspace, mode))}
    assert not paths & {".gitconfig", ".bashrc", ".vscode"} and set(READY) <= paths
    if mode == "core":
        assert ".mcp.json" in paths  # Not empty: still captured, so staging refuses it as before.


@pytest.mark.parametrize("mode", ["core", "domain"])
@pytest.mark.parametrize("directory", ["drafts", "DrAfTs"])
def test_workspace_snapshots_exclude_drafts_before_reading_or_counting(
    tmp_path, monkeypatch, mode, directory,
):
    hub = project(tmp_path)
    workspace = tmp_path / "work"
    write_files(workspace, {**READY,
        f"{directory}/task/revision-1/files/Unresolved.lean": DRAFT_MARKER * 100,
        f"{directory}/task/revision-1/files/Unresolved.md": DRAFT_MARKER,
        ".private/drafts/Unresolved.lean": DRAFT_MARKER,
        ".private/drafts/Unresolved.md": DRAFT_MARKER})
    original_put = hub.blobs.put

    def forbid_draft_body(body):
        assert DRAFT_MARKER not in body
        return original_put(body)

    monkeypatch.setattr(hub.blobs, "put", forbid_draft_body)
    sha = capture(hub, workspace, mode, max_files=len(READY),
                  max_bytes=sum(map(len, READY.values())))
    entries = read_snapshot(hub.blobs, sha)
    assert {item["path"]: hub.blobs.get(item["sha256"]) for item in entries} == READY


@pytest.mark.parametrize("mode", ["core", "domain"])
@pytest.mark.parametrize("directory", ["drafts", "DrAfTs"])
@pytest.mark.parametrize("relative", ["", "task/revision-1/files"])
def test_direct_shared_draft_capture_cannot_strip_its_reserved_prefix(
    tmp_path, monkeypatch, mode, directory, relative,
):
    hub = project(tmp_path)
    selected = tmp_path / directory / relative
    write_files(selected, {"Unresolved.lean": DRAFT_MARKER, "Unresolved.md": DRAFT_MARKER})

    def forbid_capture(_body):
        pytest.fail("A rejected draft source must not write shared blobs")

    monkeypatch.setattr(hub.blobs, "put", forbid_capture)
    with pytest.raises(ValueError, match="Shared drafts"):
        capture(hub, selected, mode)


@pytest.mark.parametrize("mode", ["core", "domain"])
def test_unrelated_drafts_ancestor_does_not_block_normal_source(tmp_path, mode):
    hub = project(tmp_path / "drafts" / "project")
    for source in (hub.root / "work", tmp_path / "external" / "drafts"):
        write_files(source, READY)
        sha = capture(hub, source, mode)
        assert {item["path"] for item in read_snapshot(hub.blobs, sha)} == READY.keys()


@pytest.mark.parametrize("mode", ["core", "domain"])
def test_editable_private_draft_root_cannot_be_captured_directly(tmp_path, mode):
    hub = project(tmp_path)
    source = tmp_path / "work" / ".private" / "drafts" / "current"
    write_files(source, READY)
    with pytest.raises(ValueError, match="Private"):
        capture(hub, source, mode)


@pytest.mark.parametrize("path", ["drafts/Task/Claim.lean", "DrAfTs/Task/Claim.md", "drafts"])
def test_forged_draft_manifest_and_direct_materialization_are_rejected(tmp_path, path):
    hub = project(tmp_path)
    sha = manifest(hub.blobs, {path: DRAFT_MARKER})
    with pytest.raises(RegistryError, match="Shared drafts"):
        read_snapshot(hub.blobs, sha)
    # The Lean service reads frozen sources through the same boundary.
    with pytest.raises(RegistryError, match="Shared drafts"):
        lean_snapshot_sources(hub.blobs, sha, max_files=MAX_SNAPSHOT_FILES, max_bytes=MAX_SNAPSHOT_BYTES)


@pytest.mark.parametrize("inferred", [False, True])
def test_publication_selection_rejects_drafts_before_staging(inferred):
    paths = ["DrAfTs/task/revision-1/files/Claim.lean"]
    plugin = (SimpleNamespace(publication_paths=lambda *_: paths) if inferred else SimpleNamespace())
    payload = {} if inferred else {"repository_paths": paths}
    with pytest.raises(HubError) as rejected:
        RepositoryCoordination()._publication_paths({}, payload, plugin)
    assert rejected.value.code == "INVALID_PATH"


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_drafts_cannot_seed_repository_or_enter_an_unselected_candidate_snapshot(tmp_path):
    hub = project(tmp_path)
    repository = RepositoryStore(hub.state_dir)
    invalid = manifest(hub.blobs, {**READY, "DrAfTs/task/Claim.md": DRAFT_MARKER})
    with pytest.raises(RepositoryError, match="Shared drafts"):
        repository.initialize(hub.blobs, invalid)
    initial = repository.initialize(hub.blobs, manifest(hub.blobs, READY))
    workspace = tmp_path / "review" / ".taskhub-integration" / "draft-check"
    with pytest.raises(RepositoryError, match="Shared drafts"):
        repository.prepare(hub.blobs, invalid, initial["head"], workspace, list(READY))
    assert repository.head() == initial["head"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_git_snapshot_rejects_draft_tree_even_when_manifest_ingress_was_bypassed(tmp_path):
    hub = project(tmp_path)
    repository = RepositoryStore(hub.state_dir)
    initial = repository.initialize(hub.blobs)
    blob = repository._hash_bytes(DRAFT_MARKER, repository.path)
    subtree = repository._git(["mktree"], git_dir=repository.path,
                              body=f"100644 blob {blob}\tClaim.md\n".encode()).decode().strip()
    tree = repository._git(["mktree"], git_dir=repository.path,
                           body=f"040000 tree {subtree}\tDrAfTs\n".encode()).decode().strip()
    commit = repository._commit(repository.path, tree, initial["head"], "Synthetic imported tree\n")
    with pytest.raises(RepositoryError, match="Invalid Git tree entry"):
        repository.snapshot(commit, hub.blobs)
    assert repository.head() == initial["head"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_ready_pair_can_be_deliberately_copied_to_normal_source_paths(tmp_path):
    hub = project(tmp_path)
    shared = tmp_path / "drafts" / "task" / "revision-1" / "files"
    write_files(shared, READY)
    workspace = tmp_path / "work"
    write_files(workspace, {name: (shared / name).read_bytes() for name in READY})
    nested = {"Math/drafts/History.md": b"A nested ordinary source path is not the shared root.\n"}
    write_files(workspace, nested)
    expected = {**READY, **nested}
    sha = snapshot_sources(hub, workspace)
    assert hub.blobs.snapshot(workspace) == sha
    entries = read_snapshot(hub.blobs, sha)
    lean_snapshot_sources(hub.blobs, sha, max_files=MAX_SNAPSHOT_FILES, max_bytes=MAX_SNAPSHOT_BYTES)
    assert RepositoryCoordination()._publication_paths(
        {}, {"repository_paths": list(expected)}, SimpleNamespace()) == sorted(expected)
    repository = RepositoryStore(hub.state_dir)
    initial = repository.initialize(hub.blobs, sha)
    assert repository.snapshot(initial["head"], hub.blobs) == sha
    assert {item["path"]: hub.blobs.get(item["sha256"]) for item in entries} == expected
