"""One public branch preserves exact review history and recoverable state exports."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from formal_geyser.coordination import sync_state
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.git_sync import GitSyncService
from formal_geyser.coordination.git_transport import (
    GitSyncError,
    GitSyncTransport,
    RemoteHeadMoved,
)
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    GitSyncConfig,
    ProjectConfig,
    RepositoryConfig,
)
from formal_geyser.coordination.repository import RepositoryStore
from formal_geyser.coordination.storage import BlobStore
from tests.coordination.lifecycle_helpers import resolve_recruitment, select_review

from .publication_helpers import approve
from .test_archive_publication import command, original_validation, quiet, worker
from .test_git_sync import case as case
from .test_git_sync import exported, publish_task, runtime_counts
from .test_git_transport import git, manifest, push_external
from .test_git_transport import remote as remote
from .test_git_transport import transport as transport


@pytest.fixture
async def scenario(tmp_path):
    """A validated document candidate awaiting its independent review."""
    initialize(tmp_path, ProjectConfig(repository=RepositoryConfig(enabled=True)))
    hub = Hub(tmp_path)
    case = SimpleNamespace(hub=hub, root=tmp_path)
    try:
        case.initial = await command(hub, "repository.initialize", {
            "snapshot": manifest(hub.blobs, {"document.md": b"Base document.\n"})})
        case.host = await command(hub, "task.publish", {"contract": {
            "title": "Publish a reviewed document", "objective": "Integrate the checked document.",
            "acceptance_policy": "general.peer@1"}})
        case.run = await command(hub, "run.start")
        case.author = await hub.next_assignment(case.run["id"])
        (hub.workspace(case.author["worker_id"]) / "document.md").write_bytes(b"Candidate document.\n")
        await command(hub, "artifact.submit", {"kind": "software.result", "data": {"ready": True},
                      "repository_paths": ["document.md"]}, worker(case.author))
        case.candidate = await quiet(hub, case.author)
        await resolve_recruitment(hub, case.run["id"])
        await original_validation(case)
        case.reviewer = await select_review(hub, case.candidate["review_task_id"], case.run["id"])
        yield case
    finally:
        await hub.close()


async def test_real_main_combines_source_wiki_and_state_across_review_publications(transport, tmp_path, remote):
    repository = RepositoryStore(tmp_path / "managed")
    blobs = BlobStore(tmp_path / "managed" / "blobs")
    source = {"README.md": b"Reviewed project introduction.\n", "document.md": b"Original.\n",
              "wiki/experience.md": b"Reviewed experience.\n", "obsolete.md": b"Remove later.\n",
              "tasks/source.md": b"This source directory is not generated state.\n",
              "sync.json": b'{"owned_by":"source"}\n'}
    base = repository.initialize(blobs, manifest(blobs, source))["head"]
    prepared = []

    async def on_prepared(commit):
        prepared.append(commit)

    state = {"sync.json": json.dumps({"main_commit": base}).encode(),
             "tasks/index.md": b"Task 0: pending.\n", "old.json": b"{}"}
    first = await transport.publish_project(repository, base, "main", state, None, "Initial project\n", on_prepared)
    assert transport.files(first["head"]) == {**source, **{"taskhub/" + k: v for k, v in state.items()}}
    assert git("--git-dir", str(remote), "for-each-ref", "--format=%(refname)", "refs/heads").splitlines() == [b"refs/heads/main"]
    assert git("--git-dir", str(remote), "show", "-s", "--format=%P", first["head"]).strip() == base.encode()
    state.pop("old.json")
    state["tasks/index.md"] = b"Task 0: working.\n"
    second = await transport.publish_project(repository, base, "main", state, first["head"], "Task progress\n", on_prepared)
    assert "taskhub/old.json" not in transport.files(second["head"])
    assert git("--git-dir", str(remote), "show", "-s", "--format=%P", second["head"]).strip() == first["head"].encode()
    assert repository.head() == base

    revised = {**source, "document.md": b"Reviewed replacement.\n", "wiki/experience.md": b"Updated experience.\n"}
    revised.pop("obsolete.md")
    workspace = tmp_path / "reviewer" / "work" / ".taskhub-integration" / "review"
    repository.prepare(blobs, manifest(blobs, revised), base, workspace, sorted(set(source) | set(revised)))
    accepted = repository.finalize(workspace, blobs)["commit"]
    repository.publish(accepted, base)
    state["sync.json"] = json.dumps({"main_commit": accepted}).encode()
    third = await transport.publish_project(repository, accepted, "main", state, second["head"], "New reviewed source\n", on_prepared)
    assert transport.files(third["head"]) == {**revised, **{"taskhub/" + k: v for k, v in state.items()}}
    assert git("--git-dir", str(remote), "show", "-s", "--format=%P", third["head"]).split() == [second["head"].encode(), accepted.encode()]
    assert git("--git-dir", str(remote), "show", f"{accepted}:document.md") == revised["document.md"]
    assert repository.head() == accepted
    assert prepared == [first["head"], second["head"], third["head"]]
    assert not any(path.startswith("taskhub/") for path in
                   repository._entries(repository.path, repository._commit_tree(repository.path, accepted))[0])


async def test_real_export_retains_executable_modes(transport, tmp_path, remote):
    repository = RepositoryStore(tmp_path / "managed")
    blobs = BlobStore(tmp_path / "managed" / "blobs")
    base = repository.initialize(blobs)["head"]
    executable = repository._hash_bytes(b"#!/bin/sh\nexit 0\n", repository.path)
    tree = repository._tree({"check.sh": ("100755", executable)}, repository.path)
    accepted = repository._commit(repository.path, tree, base, "Synthetic accepted executable\n")
    # Seed an existing public tree with an executable. Ordinary new integration
    # files currently normalize to 100644; this tests the transport's preservation
    # of a pre-existing Git mode, not review authorization.
    repository._git(["update-ref", repository.ref, accepted, base], git_dir=repository.path)

    async def prepared(commit):
        pass

    result = await transport.publish_project(repository, accepted, "main", {"README.md": b"State\n"},
                                               None, "Combined project\n", prepared)
    assert git("--git-dir", str(remote), "ls-tree", result["head"], "check.sh").startswith(b"100755 blob ")


@pytest.mark.parametrize("path", ["taskhub", "taskhub/source.md", "TaskHub/source.md"])
async def test_reserved_directory_collision_is_rejected_without_pushing(transport, tmp_path, path):
    repository = RepositoryStore(tmp_path / "managed")
    blobs = BlobStore(tmp_path / "managed" / "blobs")
    base = repository.initialize(blobs, manifest(blobs, {path: b"Reviewed content must not be overwritten."}))["head"]

    async def unexpected(commit):
        raise AssertionError("A colliding tree must not reach publication")

    with pytest.raises(GitSyncError, match="reserved taskhub"):
        await transport.publish_project(repository, base, "main", {"README.md": b"State"}, None, "State", unexpected)
    assert await transport.fetch("main") is None
    assert repository.head() == base


async def test_real_prepare_failure_never_pushes_and_can_retry(transport, tmp_path):
    repository = RepositoryStore(tmp_path / "managed")
    blobs = BlobStore(tmp_path / "managed" / "blobs")
    base = repository.initialize(blobs)["head"]
    pending = []

    async def fail(commit):
        pending.append(commit)
        raise GitSyncError("Synthetic failed durable pending receipt")

    with pytest.raises(GitSyncError, match="pending receipt"):
        await transport.publish_project(repository, base, "main", {"README.md": b"State"}, None, "State", fail)
    assert await transport.fetch("main") is None

    async def prepared(commit):
        assert commit == pending[0]

    result = await transport.publish_project(repository, base, "main", {"README.md": b"State"}, None, "State", prepared)
    assert result["head"] == pending[0]


async def test_real_combined_push_preserves_a_racing_remote_edit(transport, tmp_path, remote):
    repository = RepositoryStore(tmp_path / "managed")
    blobs = BlobStore(tmp_path / "managed" / "blobs")
    base = repository.initialize(blobs)["head"]
    outsider = GitSyncTransport(tmp_path / "outsider", str(remote))
    outsider.initialize()
    foreign = None

    async def race(commit):
        nonlocal foreign
        foreign = await push_external(outsider, remote, {"important.md": b"Preserve this."})

    with pytest.raises(RemoteHeadMoved):
        await transport.publish_project(repository, base, "main", {"README.md": b"State"}, None, "State", race)
    assert await transport.fetch("main") == foreign
    assert transport.files(foreign) == {"important.md": b"Preserve this."}


async def test_recovery_requires_exact_pending_commit_even_with_identical_files(case):
    case.transport.lost_reply = True
    assert (await case.service.once())["state"] == "error"
    pending = case.transport.branches["main"]
    foreign = case.transport.seed("main", case.transport.files(pending))
    assert foreign != pending
    result = await case.service.once()
    assert result["state"] == "error" and result["error"]["code"] == "RemoteHeadMoved"
    assert case.transport.branches["main"] == foreign
    assert (await sync_state.state(case.hub))["pending_head"] == pending


async def test_recovery_can_then_export_new_events_without_duplicate_source(case):
    case.transport.lost_reply = True
    assert (await case.service.once())["state"] == "error"
    first = case.transport.branches["main"]
    task = await publish_task(case.hub, title="New task after interrupted sync")
    result = await GitSyncService(case.hub, transport=case.transport).once()
    assert result["state"] == "synced" and result["main"]["head"] != first
    assert f"tasks/{task['id']}/task.json" in exported(case.transport)
    assert (await case.hub.repository_status())["head"] == case.base


async def test_state_updates_do_not_overwrite_source_and_unchanged_export_does_not_commit(case):
    first = await case.service.once()
    task = await publish_task(case.hub, title="Second shared task")
    changed = await case.service.once()
    assert changed["state"] == "synced" and changed["main"]["head"] != first["main"]["head"]
    files = case.transport.files(changed["main"]["head"])
    assert files["document.md"] == b"Reviewed base document.\n"
    assert f"taskhub/tasks/{task['id']}/task.json" in files
    assert set(case.transport.branches) == {"main"}
    assert (await case.service.once())["main"] == {"head": changed["main"]["head"], "changed": False}
    status = await case.service.status()
    assert status["branch"] == "main" and status["state_directory"] == "taskhub"
    assert "state_branch" not in status
    assert await case.hub.list_runs() == []


async def test_native_review_publication_syncs_only_after_verified_integration(scenario, tmp_path):
    remote = tmp_path / "public.git"
    git("init", "--bare", "--template=", "--initial-branch=main", str(remote))
    hub = scenario.hub
    hub.config.sync = GitSyncConfig(enabled=True, remote=str(remote))
    transport = GitSyncTransport(hub.state_dir, str(remote))
    service = GitSyncService(hub, transport=transport)
    before = await runtime_counts(hub)
    first = await service.once()
    assert first["state"] == "synced", first
    assert first["source_commit"] == scenario.initial["head"]
    assert transport.files(first["main"]["head"])["document.md"] == b"Base document.\n"
    assert await runtime_counts(hub) == before

    await approve(hub, scenario.reviewer)
    source = (await hub.repository_status())["head"]
    assert source != scenario.initial["head"]
    before = await runtime_counts(hub)
    published = await service.once()
    assert published["state"] == "synced" and published["source_commit"] == source
    assert transport.files(published["main"]["head"])["document.md"] == b"Candidate document.\n"
    assert json.loads(exported(transport, published["main"]["head"])["sync.json"])["main_commit"] == source
    assert await runtime_counts(hub) == before
    assert (await service.once())["main"] == {"head": published["main"]["head"], "changed": False}

