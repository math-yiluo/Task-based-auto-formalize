"""Native source publication, public ledger export, and recoverable optional Git sync."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination import sync_state
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.git_sync import GitSyncService
from formal_geyser.coordination.git_transport import (
    GitSyncError,
    GitSyncTransport,
    RemoteHeadMoved,
)
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    GitSyncConfig,
    ProjectConfig,
    RepositoryConfig,
    Role,
)
from formal_geyser.coordination.plugins import GeneralPlugin
from formal_geyser.coordination.storage import canonical
from tests.coordination.test_git_transport import push_external

OPERATOR = Actor(role=Role.OPERATOR)
BRANCH = "contrib/test-bot"


def json_bytes(value) -> bytes:
    return (canonical(value) + "\n").encode("utf-8")


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), OPERATOR)


def snapshot(hub, contents):
    return hub.blobs.put_json({"files": [
        {"path": name, "sha256": hub.blobs.put(body)} for name, body in sorted(contents.items())
    ]})


async def publish_task(hub, *, policy="general.peer@1", title="Shared software task"):
    contract = {"title": title, "objective": "Produce an independently reviewed document.",
                "context": "The exact baseline is exported on the public source branch.",
                "acceptance_policy": policy, "constraints": ["Keep evidence exact."]}
    if policy == "general.json@1":
        contract["payload"] = {"output_schema": {"type": "object", "required": ["ready"],
                                                 "properties": {"ready": {"type": "boolean"}}}}
    return await command(hub, "task.publish", {"contract": contract})


class MemoryTransport:
    """A deterministic remote double; real repository authority remains in the Hub."""

    def __init__(self):
        self.branches = {}
        self.commits = {}
        self.initialized = 0
        self.calls = []
        self.unavailable = set()
        self.lost_reply = False

    def initialize(self):
        self.initialized += 1

    def seed(self, branch, contents, *, commit=None):
        commit = commit or hashlib.sha1(json_bytes({
            "nonce": uuid4().hex, "files": sync_state.file_digest(contents),
        })).hexdigest()
        self.branches[branch] = commit
        self.commits[commit] = dict(contents)
        return commit

    async def fetch(self, branch):
        self.calls.append(("fetch", branch))
        if "*" in self.unavailable or branch in self.unavailable:
            raise GitSyncError("Synthetic offline remote")
        return self.branches.get(branch)

    def files(self, commit):
        return dict(self.commits[commit])

    async def _publish(self, branch, files, expected_head, message):
        if self.branches.get(branch) != expected_head:
            raise RemoteHeadMoved("Synthetic remote changed")
        if expected_head and self.commits[expected_head] == files:
            return {"head": expected_head, "changed": False}
        head = hashlib.sha1(json_bytes({"parent": expected_head, "message": message,
                                       "files": sync_state.file_digest(files)})).hexdigest()
        self.seed(branch, files, commit=head)
        if self.lost_reply:
            self.lost_reply = False
            raise GitSyncError("Synthetic lost state-push reply")
        return {"head": head, "changed": True}

    async def publish_project(self, repository, source_commit, branch, state_files,
                              expected_head, message, on_prepared):
        self.calls.append(("publish_project", branch))
        with repository._locked():
            entries, bodies = repository._entries(repository.path,
                repository._commit_tree(repository.path, source_commit))
        files = {name: bodies[sha] for name, (_, sha) in entries.items()}
        if any(name.split("/", 1)[0].casefold() == "taskhub" for name in files):
            raise GitSyncError("Reviewed source uses reserved taskhub/")
        files.update({"taskhub/" + name: body for name, body in state_files.items()})
        head = hashlib.sha1(json_bytes({"parent": expected_head, "message": message,
                                       "files": sync_state.file_digest(files)})).hexdigest()
        await on_prepared(head)
        return await self._publish(branch, files, expected_head, message)


@pytest.fixture
async def case(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Native Git is required for the managed publication repository")
    root, remote = tmp_path / "project", tmp_path / "remote.git"
    initialize(root, ProjectConfig(
        name="Synthetic shared project", repository=RepositoryConfig(enabled=True),
        sync=GitSyncConfig(enabled=True, remote=str(remote)), environment={"private_operator_setting": "synthetic-config-secret"},
    ))
    hub = Hub(root)
    try:
        base = await command(hub, "repository.initialize", {
            "snapshot": snapshot(hub, {"document.md": b"Reviewed base document.\n"}),
        })
        task = await publish_task(hub)
        transport = MemoryTransport()
        yield SimpleNamespace(hub=hub, root=root, remote=remote, base=base["head"], task=task,
                              transport=transport, service=GitSyncService(hub, transport=transport))
    finally:
        await hub.close()


async def runtime_counts(hub):
    return await hub.db.call(lambda c: {
        table: c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        for table in ("runs", "workers", "attempts", "turn_intents", "evidence")
    })


def forbid_validation(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Git synchronization must not run a validator or compiler")
    monkeypatch.setattr(GeneralPlugin, "validate_candidate", unexpected)


def exported(transport, head=None):
    files = transport.files(head or transport.branches["main"])
    return {name.removeprefix("taskhub/"): body for name, body in files.items()
            if name.startswith("taskhub/")}


async def test_offline_remote_records_error_and_recovers_without_runtime_execution(case):
    case.transport.unavailable.add("*")
    offline = await case.service.once()
    assert offline["state"] == "error" and "offline" in offline["error"]["message"]
    assert (await case.service.status())["last_error"]
    assert not case.transport.branches
    case.transport.unavailable.clear()
    assert (await case.service.once())["state"] == "synced"
    assert (await case.service.status())["last_error"] is None
    assert all(value == 0 for value in (await runtime_counts(case.hub)).values())


async def test_foreign_public_branch_is_preserved(case):
    foreign = case.transport.seed("main", {"taskhub/sync.json": json_bytes({"project_id": "another-project"}),
                                           "foreign.md": b"Must remain untouched.\n"})
    before = dict(case.transport.commits[foreign])
    result = await case.service.once()
    assert result["state"] == "error", result
    assert case.transport.branches["main"] == foreign
    assert case.transport.files(foreign) == before
    assert (await case.hub.repository_status())["head"] == case.base
    assert ("publish_project", "main") not in case.transport.calls


@pytest.mark.parametrize("path", ["document.md", "taskhub/README.md"])
async def test_an_operator_edit_to_previously_exported_state_is_not_replaced(case, path):
    first = await case.service.once()
    contents = case.transport.files(first["main"]["head"])
    contents[path] += b"Independent operator edit.\n"
    changed = case.transport.seed("main", contents)
    result = await case.service.once()
    assert result["state"] == "error" and result["error"]["code"] == "RemoteHeadMoved"
    assert case.transport.branches["main"] == changed
    assert (await case.service.status())["main_head"] == first["main"]["head"]


async def test_lost_state_push_acknowledgement_recovers_exact_commit_and_sequence(case):
    case.transport.lost_reply = True
    failed = await case.service.once()
    assert failed["state"] == "error"
    successful_remote_head = case.transport.branches["main"]
    marker = json.loads(exported(case.transport)["sync.json"])
    metadata = await sync_state.state(case.hub)
    assert metadata["main_head"] is None and metadata["pending_digest"]
    assert metadata["pending_head"] == successful_remote_head
    before = len(case.transport.commits)
    restarted = GitSyncService(case.hub, transport=case.transport)
    result = await restarted.once()
    assert result["state"] == "synced", result
    assert result["main"] == {"head": successful_remote_head, "changed": False}
    metadata = await sync_state.state(case.hub)
    assert metadata["last_export_sequence"] == marker["ledger_sequence"]
    assert metadata["pending_digest"] is None and metadata["pending_head"] is None
    assert metadata["last_error"] is None
    assert len(case.transport.commits) == before


async def test_export_omits_credentials_private_bodies_workspaces_and_unselected_blobs(case):
    private_body = b"synthetic-private-artifact-body"
    private_workspace = b"synthetic-private-workspace-text"
    private_pdf = b"%PDF-1.7 synthetic-private-upload-content"
    (case.hub.workspace("offline-diagnostic") / "private.txt").write_bytes(private_workspace)
    case.hub.blobs.put(private_pdf)

    def private(c):
        artifact = case.hub._insert_artifact(c, "software.private", {"secret": private_body.decode()},
                                            producer="synthetic-operator")
        c.execute("UPDATE artifacts SET status='private' WHERE id=?", (artifact["id"],))
        case.hub._event(c, "record.private", artifact["id"])
        return artifact["id"]

    private_id = await case.hub.db.call(private, write=True)
    public = await command(case.hub, "artifact.register", {"kind": "software.input", "data": {"shared": "Public input"}})
    before = await case.hub.events()
    contents, sequence = await sync_state.export_files(case.hub, main_commit=case.base)
    combined = b"\n".join(contents.values())
    for secret in (private_body, private_workspace, private_pdf, b"synthetic-config-secret"):
        assert secret not in combined
    assert f"artifacts/{private_id}.json" not in contents
    assert json.loads(contents[f"notes/{public['id']}.json"])["data"] == {"shared": "Public input"}
    assert json.loads(contents["artifacts/index.json"]) == []
    assert {row["id"] for row in json.loads(contents["notes/index.json"])} == {public["id"]}
    assert not any(name.startswith((".task-based/", "workers/", "objects/")) for name in contents)
    assert json.loads(contents["sync.json"])["ledger_sequence"] == sequence
    assert await case.hub.events() == before


async def test_export_rejects_a_mismatched_public_source_commit(case):
    with pytest.raises(HubError) as error:
        await sync_state.export_files(case.hub, main_commit="f" * 40)
    assert error.value.code == "SYNC_SOURCE_MOVED"


async def test_remote_binding_cannot_change_after_the_ledger_has_synced(case, tmp_path):
    assert (await case.service.once())["state"] == "synced"
    case.hub.config.sync = GitSyncConfig(enabled=True, remote=str(tmp_path / "different.git"))
    other = MemoryTransport()
    result = await GitSyncService(case.hub, transport=other).once()
    assert result["state"] == "error" and result["error"]["code"] == "SYNC_REMOTE_CHANGED"
    assert not other.calls and other.initialized == 0


async def test_disabled_default_performs_no_transport_work(tmp_path):
    assert GitSyncConfig().enabled is False and GitSyncConfig().remote is None
    hub = Hub(tmp_path, config=ProjectConfig())
    try:
        transport = MemoryTransport()
        service = GitSyncService(hub, transport=transport)
        assert await service.once() == {"enabled": False, "state": "disabled"}
        await service.run()
        assert transport.initialized == 0 and not transport.calls and not transport.branches
        assert all(value == 0 for value in (await runtime_counts(hub)).values())
    finally:
        await hub.close()


@pytest.mark.parametrize("remote", ["https://token@github.com/owner/repo", "ext::shell command",
                                     "../ambiguous", "https://github.com/owner/repo?token=secret"])
def test_sync_config_rejects_credentials_and_command_remotes(remote):
    with pytest.raises(ValueError):
        GitSyncConfig(remote=remote)


@pytest.mark.parametrize("helper", ["!echo secret", "manager --token=secret", "name;command"])
def test_sync_config_rejects_credential_helper_commands(helper):
    with pytest.raises(ValueError):
        GitSyncConfig(credential_helper=helper)


def test_sync_config_requires_explicit_enablement_and_repository():
    assert GitSyncConfig(remote="owner/repo").enabled is False
    with pytest.raises(ValueError):
        GitSyncConfig(enabled=True)
    with pytest.raises(ValueError):
        ProjectConfig(sync=GitSyncConfig(enabled=True, remote="owner/repo"))


@pytest.mark.parametrize("contributors", [[], [{"id": "author", "branch": "contrib/one"}]])
def test_sync_config_rejects_removed_contributor_fields(contributors):
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        GitSyncConfig.model_validate({"contributors": contributors})
    assert "contributors" not in GitSyncConfig().model_dump()
    assert "contributors" not in GitSyncConfig.model_json_schema()["properties"]


async def test_real_local_git_publishes_source_and_state_without_polling_other_branches(case, tmp_path, monkeypatch):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    subprocess.run(["git", "init", "--bare", "--template=", "--initial-branch=main", str(case.remote)],
                   check=True, capture_output=True, timeout=15, env=environment)
    forbid_validation(monkeypatch)
    transport = GitSyncTransport(case.hub.state_dir, str(case.remote))
    fetched = []
    original_fetch = transport.fetch

    async def fetch(branch):
        fetched.append(branch)
        return await original_fetch(branch)

    monkeypatch.setattr(transport, "fetch", fetch)
    service = GitSyncService(case.hub, transport=transport)
    first = await service.once()
    assert first["state"] == "synced", first
    assert first["main"]["changed"] and "taskhub" not in first
    assert first["source_commit"] == case.base
    assert transport.files(first["main"]["head"])["document.md"] == b"Reviewed base document.\n"
    initial_export = exported(transport, first["main"]["head"])
    assert {"sync.json", "tasks/index.md", f"tasks/{case.task['id']}/task.json",
            f"tasks/{case.task['id']}/context.md", f"tasks/{case.task['id']}/progress.md"} <= set(initial_export)
    assert json.loads(initial_export["sync.json"])["main_commit"] == case.base
    assert "contributors" not in json.loads(initial_export["sync.json"])
    assert "DOTS.md" not in initial_export and "feedback.json" not in initial_export
    assert not any(path.startswith(("protocol/", "tooling/")) for path in initial_export)
    assert "imports" not in first and "contributors" not in await service.status()

    # A pre-existing remote branch and legacy protocol files are inert data.
    other = GitSyncTransport(tmp_path / "other-client", str(case.remote))
    other.initialize()
    other_files = {"document.md": b"Unreviewed replacement.\n",
                   ".taskhub-contributions/legacy/contribution.json": b"not valid JSON",
                   ".taskhub-contributions/legacy/records/report.json": b'{"id":"report"}'}
    ignored = await push_external(other, case.remote, other_files, branch=BRANCH)
    before_runtime, before_events = await runtime_counts(case.hub), await case.hub.events()
    second = await service.once()
    assert second["state"] == "synced", second
    assert second["main"] == {"head": first["main"]["head"], "changed": False}
    assert exported(transport, second["main"]["head"]) == initial_export
    assert fetched == ["main", "main"]
    assert await other.fetch(BRANCH) == ignored
    assert other.files(ignored) == other_files
    assert await case.hub.events() == before_events
    assert await runtime_counts(case.hub) == before_runtime
    assert all(value == 0 for value in before_runtime.values())
    assert (await case.hub.repository_status())["head"] == case.base


async def test_other_branch_unavailability_is_irrelevant_to_native_publication(case):
    case.transport.unavailable.add(BRANCH)
    result = await case.service.once()
    assert result["state"] == "synced", result
    assert case.transport.calls == [("fetch", "main"), ("publish_project", "main")]
    assert (await case.service.status())["last_error"] is None
    assert "imports" not in result and "errors" not in result


async def test_lean_publication_exports_native_source_without_protocol_assets_or_execution(tmp_path, monkeypatch):
    from formal_geyser.coordination.runtime.codex import CodexHarness
    from formal_geyser.plugin import LeanPlugin
    from formal_geyser.tooling import LeanTooling

    if shutil.which("git") is None:
        pytest.skip("Native Git is required for the managed publication repository")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Optional source synchronization cannot execute Lean or models")

    monkeypatch.setattr(LeanTooling, "compile", forbidden)
    monkeypatch.setattr(LeanTooling, "doctor", forbidden)
    monkeypatch.setattr(LeanPlugin, "validate_candidate", forbidden)
    monkeypatch.setattr(CodexHarness, "open", forbidden)
    root = tmp_path / "lean-project"
    initialize(root, ProjectConfig(domain="lean", repository=RepositoryConfig(enabled=True),
        sync=GitSyncConfig(enabled=True, remote=str(tmp_path / "remote.git"))))
    hub = Hub(root)
    try:
        source = {"Fixture.lean": b"-- Synthetic source publication fixture only.\n",
                  "Fixture.md": b"Synthetic storage fixture; no mathematical claim is asserted.\n"}
        base = await command(hub, "repository.initialize", {"snapshot": snapshot(hub, source)})
        transport = MemoryTransport()
        result = await GitSyncService(hub, transport=transport).once()
        assert result["state"] == "synced", result
        assert result["source_commit"] == base["head"]
        files = transport.files(result["main"]["head"])
        assert all(files[path] == body for path, body in source.items())
        shared = exported(transport)
        assert "DOTS.md" not in shared and "feedback.json" not in shared
        assert not any(path.startswith(("protocol/", "tooling/")) for path in shared)
        assert not any(path.startswith("library/") for path in shared)
        assert all(value == 0 for value in (await runtime_counts(hub)).values())
    finally:
        await hub.close()
