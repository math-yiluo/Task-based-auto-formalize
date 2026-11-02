"""Advisory shared graphs remain outside formal source and publication evidence."""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.content import (
    ContentError,
    FreshTree,
    NativeContentStore,
    verify_references,
)
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.graph_paths import (
    GRAPH_DIRECTORY,
    is_graph_path,
    is_shared_graph_directory,
)
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.integration import RepositoryCoordination
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    RepositoryConfig,
    Role,
)
from formal_geyser.coordination.repository import RepositoryError, RepositoryStore
from formal_geyser.coordination.snapshot_limits import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
)
from formal_geyser.coordination.storage import BlobStore
from formal_geyser.coordination.sync_state import export_files
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
GRAPH_MARKER = b"advisory-shared-graph-marker"
GRAPHS = {"definitions.json": b'{"nodes": [], "edges": []}\n',
          "proofs.json": b'{"nodes": [], "edges": []}\n'}


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
@pytest.mark.parametrize("directory", ["graphs", "GrApHs"])
def test_workspace_snapshots_exclude_graphs_before_reading_or_counting(
    tmp_path, monkeypatch, mode, directory,
):
    hub = project(tmp_path)
    workspace = tmp_path / "work"
    write_files(workspace, {**READY,
        f"{directory}/current.json": GRAPH_MARKER,
        f"{directory}/revision-1-key/files/definitions.json": GRAPH_MARKER * 100,
        f"{directory}/revision-1-key/files/proofs.json": GRAPH_MARKER,
        "drafts/task/revision-1/files/Draft.md": GRAPH_MARKER,
        ".private/graphs/proofs.json": GRAPH_MARKER})
    original_put = hub.blobs.put

    def forbid_graph_body(body):
        assert GRAPH_MARKER not in body
        return original_put(body)

    monkeypatch.setattr(hub.blobs, "put", forbid_graph_body)
    sha = capture(hub, workspace, mode, max_files=len(READY),
                  max_bytes=sum(map(len, READY.values())))
    entries = read_snapshot(hub.blobs, sha)
    assert {item["path"]: hub.blobs.get(item["sha256"]) for item in entries} == READY


@pytest.mark.parametrize("mode", ["core", "domain"])
def test_root_file_named_graphs_is_excluded_from_snapshot(tmp_path, mode):
    hub = project(tmp_path)
    workspace = tmp_path / "work"
    write_files(workspace, {**READY, "GrApHs": GRAPH_MARKER})
    sha = capture(hub, workspace, mode, max_files=len(READY),
                  max_bytes=sum(map(len, READY.values())))
    assert {item["path"] for item in read_snapshot(hub.blobs, sha)} == READY.keys()


@pytest.mark.parametrize("mode", ["core", "domain"])
@pytest.mark.parametrize("directory", ["graphs", "GrApHs"])
@pytest.mark.parametrize("relative", ["", "revision-1-key", "revision-1-key/files"])
def test_direct_shared_graph_capture_cannot_strip_its_reserved_prefix(
    tmp_path, monkeypatch, mode, directory, relative,
):
    hub = project(tmp_path)
    selected = tmp_path / directory / relative
    write_files(selected, GRAPHS)
    assert is_shared_graph_directory(selected, hub.root)
    assert is_shared_graph_directory(selected, hub.root)

    def forbid_capture(_body):
        pytest.fail("A rejected graph source must not write shared blobs")

    monkeypatch.setattr(hub.blobs, "put", forbid_capture)
    with pytest.raises(ValueError, match="Shared graphs"):
        capture(hub, selected, mode)


@pytest.mark.parametrize("mode", ["core", "domain"])
def test_unrelated_graphs_ancestor_does_not_block_normal_source(tmp_path, mode):
    hub = project(tmp_path / "graphs" / "project")
    assert not is_shared_graph_directory(hub.root, hub.root)
    for source in (hub.root / "work", tmp_path / "external" / "graphs"):
        write_files(source, READY)
        assert not is_shared_graph_directory(source, hub.root)
        sha = capture(hub, source, mode)
        assert {item["path"] for item in read_snapshot(hub.blobs, sha)} == READY.keys()


@pytest.mark.parametrize("path", [
    "graphs/current.json", "GrApHs/revision-1-key/files/definitions.json",
    "graphs/revision-1-key/files/proofs.json", "graphs",
])
def test_explicit_graph_manifest_and_direct_materialization_are_rejected(tmp_path, path):
    hub = project(tmp_path)
    assert GRAPH_DIRECTORY == "graphs"
    assert is_graph_path(path)
    sha = manifest(hub.blobs, {path: GRAPH_MARKER})
    with pytest.raises(RegistryError, match="Shared graphs"):
        read_snapshot(hub.blobs, sha)
    # The Lean service reads frozen sources through the same boundary.
    with pytest.raises(RegistryError, match="Shared graphs"):
        lean_snapshot_sources(hub.blobs, sha, max_files=MAX_SNAPSHOT_FILES, max_bytes=MAX_SNAPSHOT_BYTES)


@pytest.mark.parametrize("inferred", [False, True])
def test_publication_selection_rejects_graphs_before_staging(inferred):
    paths = ["GrApHs/revision-1-key/files/proofs.json"]
    plugin = (SimpleNamespace(publication_paths=lambda *_: paths) if inferred else SimpleNamespace())
    payload = {} if inferred else {"repository_paths": paths}
    with pytest.raises(HubError) as rejected:
        RepositoryCoordination()._publication_paths({}, payload, plugin)
    assert rejected.value.code == "INVALID_PATH"


@pytest.mark.parametrize("directory", ["graphs", "GrApHs"])
def test_imported_library_views_reject_graph_source(tmp_path, directory):
    hub = project(tmp_path)
    content = NativeContentStore(hub.state_dir)
    names = [f"{directory}/revision-1-key/files/definitions.json"]
    sha = manifest(hub.blobs, {names[0]: GRAPH_MARKER})
    imported_repository = SimpleNamespace(snapshot=lambda *_: sha)
    with pytest.raises(ContentError, match="Shared graphs"):
        content.public_view(imported_repository, hub.blobs, "1" * 40)
    assert not content.library_root.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_graphs_cannot_seed_repository_or_enter_unselected_candidate_snapshot(tmp_path):
    hub = project(tmp_path)
    repository = RepositoryStore(hub.state_dir)
    invalid = manifest(hub.blobs, {**READY, "GrApHs/current.json": GRAPH_MARKER})
    with pytest.raises(RepositoryError, match="Shared graphs"):
        repository.initialize(hub.blobs, invalid)
    initial = repository.initialize(hub.blobs, manifest(hub.blobs, READY))
    workspace = tmp_path / "review" / ".taskhub-integration" / "graph-check"
    with pytest.raises(RepositoryError, match="Shared graphs"):
        repository.prepare(hub.blobs, invalid, initial["head"], workspace, list(READY))
    assert repository.head() == initial["head"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_git_snapshot_rejects_graph_tree_when_manifest_ingress_was_bypassed(tmp_path):
    hub = project(tmp_path)
    repository = RepositoryStore(hub.state_dir)
    initial = repository.initialize(hub.blobs)
    blob = repository._hash_bytes(GRAPH_MARKER, repository.path)
    subtree = repository._git(["mktree"], git_dir=repository.path,
                              body=f"100644 blob {blob}\tproofs.json\n".encode()).decode().strip()
    tree = repository._git(["mktree"], git_dir=repository.path,
                           body=f"040000 tree {subtree}\tGrApHs\n".encode()).decode().strip()
    commit = repository._commit(repository.path, tree, initial["head"], "Synthetic imported tree\n")
    with pytest.raises(RepositoryError, match="Invalid Git tree entry"):
        repository.snapshot(commit, hub.blobs)
    assert repository.head() == initial["head"]


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
def test_normal_source_and_nested_graph_names_remain_available(tmp_path):
    hub = project(tmp_path)
    content = NativeContentStore(hub.state_dir)
    workspace = hub.state_dir / "workers" / "worker" / "work"
    expected = {**READY, "Math/graphs/History.md": b"An ordinary nested source path.\n",
                "graphs.json": b"A filename that does not name the shared graph root.\n"}
    write_files(workspace, expected)
    assert not any(is_graph_path(name) for name in expected)
    sha = snapshot_sources(hub, workspace)
    assert hub.blobs.snapshot(workspace) == sha
    read_snapshot(hub.blobs, sha)
    lean_snapshot_sources(hub.blobs, sha, max_files=MAX_SNAPSHOT_FILES, max_bytes=MAX_SNAPSHOT_BYTES)
    assert RepositoryCoordination()._publication_paths(
        {}, {"repository_paths": list(expected)}, SimpleNamespace()) == sorted(expected)
    repository = RepositoryStore(hub.state_dir)
    initial = repository.initialize(hub.blobs, sha)
    assert repository.snapshot(initial["head"], hub.blobs) == sha
    public = content.public_view(repository, hub.blobs, initial["head"])
    assert {item.path: (public.root / item.path).read_bytes() for item in public.files} == expected


def test_native_graph_revisions_can_be_retained_outside_library(tmp_path):
    content = NativeContentStore(tmp_path / ".task-based")
    target = content.project_root / GRAPH_DIRECTORY / "revision-1-key"
    view = content._tree(target, GRAPHS, {"kind": "advisory-graphs"})
    verify_references(view.root, view.files)
    assert {item.path: (view.root / item.path).read_bytes() for item in view.files} == GRAPHS
    assert not content.library_root.exists()


def test_failed_graph_revision_cleans_its_native_temporary_directory(tmp_path, monkeypatch):
    content = NativeContentStore(tmp_path / ".task-based")
    graph_root = content.project_root / GRAPH_DIRECTORY
    target = graph_root / "revision-1-key"

    def fail_verification(*_args):
        raise ContentError("Simulated graph verification failure")

    # A fresh tree is verified as it is read back after its flush.
    monkeypatch.setattr(FreshTree, "verify", fail_verification)
    with pytest.raises(ContentError, match="Simulated graph verification failure"):
        content._tree(target, GRAPHS, {"kind": "advisory-graphs"})
    assert not target.exists()
    assert list(graph_root.iterdir()) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")
async def test_git_export_omits_local_graph_update_without_changing_public_state(tmp_path):
    initialize(tmp_path, ProjectConfig(name="Graph export boundary",
                                      repository=RepositoryConfig(enabled=True)))
    hub = Hub(tmp_path)
    try:
        initial = await hub.execute(Command(name="repository.initialize", key="graph-export-seed",
            payload={"snapshot": manifest(hub.blobs, {"document.md": b"Ordinary source.\n"})}),
            Actor(role=Role.OPERATOR))
        before, sequence = await export_files(hub, main_commit=initial["head"])
        await hub.db.call(lambda connection: hub._event(
            connection, "dags.updated", "shared-dags", {"graphs": {
                "root": str(tmp_path / "graphs" / "revision-1-key" / "files"),
                "summary": GRAPH_MARKER.decode(),
            }}), write=True)
        after, current_sequence = await export_files(hub, main_commit=initial["head"])
        assert after == before
        assert current_sequence == sequence
        assert GRAPH_MARKER not in b"\n".join(after.values())
        assert any(row["kind"] == "dags.updated" for row in await hub.events())
    finally:
        await hub.close()
