"""Worker tools exercised through fenced core actors and fake compiler output only."""
import hashlib
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    Role,
    TaskContract,
)
from formal_geyser.coordination.runtime import ScriptedHarness, WorkerRunner
from formal_geyser.coordination.storage import digest
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import RegistryError, snapshot_sources
from formal_geyser.schemas import StatementRecord
from formal_geyser.worker_tools import handle_worker_tool
from tests.bootstrap_helpers import synthetic_execution
from tests.coordination.publication_helpers import new_hub


@pytest.fixture
async def active_worker(tmp_path):
    hub = await new_hub(tmp_path, config=ProjectConfig(domain="lean"))
    synthetic_execution(hub)
    operator = Actor(role=Role.OPERATOR)
    async def execute(name, payload):
        return await hub.execute(Command(name=name, key=str(uuid4()), payload=payload), operator)
    contract = TaskContract(title="Synthetic metadata", objective="Exercise worker software tools.", domain="lean",
                            acceptance_policy="lean.peer@1")
    task = await execute("task.publish", {"contract": contract.model_dump(mode="json")})
    run = await execute("run.start", {})
    worker = await execute("worker.register", {})
    attempt = await execute("task.claim", {"task_id": task["id"], "revision": task["revision"],
                                           "worker_id": worker["id"], "run_id": run["id"]})
    actor = Actor(role=Role.WORKER, worker_id=worker["id"], attempt_id=attempt["id"],
                  fencing_token=attempt["fencing_token"])
    try:
        yield hub, actor
    finally:
        await hub.close()


async def stage_source_file(hub, actor, path, *, key, record=None, runner=None):
    """Stage paired statement files; compilation remains explicitly unavailable."""
    workspace = hub.workspace(actor.worker_id)
    body = (workspace / path).read_bytes()
    module = Path(path).stem.capitalize()
    lean_path, markdown_path = module + ".lean", module + ".md"
    (workspace / lean_path).write_text("-- Synthetic proposition-definition fixture.\ndef fixture : Prop := True\n", encoding="utf-8")
    (workspace / markdown_path).write_bytes(body)
    if record is None:
        environments = await hub.list_artifacts(kind="lean.environment")
        environment = environments[0] if environments else await _approved_environment(hub, enabled=False)
        data = {"name": Path(path).name, "statement": "Synthetic proposition-definition protocol fixture.",
                "entry_module": module, "declaration": "fixture", "environment_artifact": environment["id"],
                "provenance": "internal", "provenance_rationale": "Test native file staging without running Lean."}
    else:
        data = record.model_dump(mode="json")
    manifest_path = ".private/source-submissions/" + key + ".json"
    manifest = workspace / manifest_path
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "kind": "lean.statement", "data": data,
        "repository_paths": [lean_path, markdown_path], "introduction": "Inspect the exact synthetic source pair.",
    }), encoding="utf-8")
    attempt = await hub.authorize_worker(actor)
    runner = runner or WorkerRunner(hub, ScriptedHarness)
    return await runner._tool(attempt, "synthetic-source-thread", "taskhub_call",
                              {"operation": "stage", "arguments": {"manifest_path": manifest_path}}, key)


async def test_worker_file_stage_and_query_without_operator_lock(active_worker):
    hub, actor = active_worker
    (hub.workspace(actor.worker_id) / "source.txt").write_text("Software source fixture.", encoding="utf-8")
    imported = await stage_source_file(hub, actor, "source.txt", key="import-1")
    assert imported["kind"] == "lean.statement"
    assert imported["accepted"] is False
    record = (await hub.artifact(imported["id"]))["data"]
    successor = StatementRecord.model_validate(record).model_copy(update={"version": 2, "supersedes": imported["id"]})
    staged = await stage_source_file(hub, actor, "source.txt", key="stage-1", record=successor)
    assert staged["data"]["version"] == 2
    assert imported["review_task_id"] != staged["review_task_id"]
    assert not staged["accepted"]
    for candidate in (imported, staged):
        root = Path(candidate["content_file"]["root"]) / "files"
        assert (root / "Source.md").read_text(encoding="utf-8") == "Software source fixture."
        assert "def fixture : Prop" in (root / "Source.lean").read_text(encoding="utf-8")
        assert candidate["repository"]["paths"] == ["Source.lean", "Source.md"]
        assert (await hub.task(candidate["review_task_id"]))["state"] != "done"
    attempt = await hub.authorize_worker(actor)
    listed = await WorkerRunner(hub, ScriptedHarness)._tool(
        attempt, "query-thread", "taskhub_call",
        {"operation": "lean_registry_list", "arguments": {"object_type": "statement", "limit": 1}}, "list-1")
    assert listed["total"] == 2
    assert len(listed["items"]) == 1
    assert listed["next_offset"] == 1


async def test_worker_tool_requires_current_fence(active_worker):
    hub, actor = active_worker
    stale = actor.model_copy(update={"fencing_token": actor.fencing_token + 1})
    with pytest.raises(HubError):
        await handle_worker_tool(hub, "lean_registry_list", {}, stale, key="stale")


@pytest.mark.parametrize("path", ["../outside.txt", "C:/outside.txt", "nested/../outside.txt"])
async def test_worker_cannot_check_sources_outside_workspace(active_worker, path):
    hub, actor = active_worker
    with pytest.raises(ValidationError):
        await handle_worker_tool(hub, "lean_check", {"directory": path,
            "environment_artifact": "environment-1", "modules": ["Utility"]}, actor, key="outside")


async def test_registry_stage_does_not_grant_acceptance_authority(active_worker):
    hub, actor = active_worker
    (hub.workspace(actor.worker_id) / "source.txt").write_bytes(b"source")
    staged = await stage_source_file(hub, actor, "source.txt", key="source")
    assert staged["status"] == "staged" and not staged["accepted"]
    assert staged["review_task_id"]
    with pytest.raises(HubError, match="principal"):
        await hub.execute(Command(name="artifact.accept", key="unauthorized", payload={
            "artifact_id": staged["id"], "verdict": "accepted", "policy": "lean.statement@1",
        }), actor)


@pytest.mark.parametrize("name", ["lean_import_source", "lean_stage_record", "lean_snapshot"])
async def test_retired_registry_tools_have_no_dispatch_route(active_worker, name):
    hub, actor = active_worker
    source = hub.workspace(actor.worker_id) / "source.txt"
    source.write_bytes(b"source")
    record = {"object_type": "source", "name": "Source", "media_type": "text/plain"}
    arguments = {"path": "source.txt"} if name == "lean_import_source" else {"record": record}
    with pytest.raises(RegistryError, match="Unknown Lean worker tool"):
        await handle_worker_tool(hub, name, arguments, actor, key="historical-source")
    attempt = await hub.authorize_worker(actor)
    runner = WorkerRunner(hub, ScriptedHarness)
    direct = await runner._tool(attempt, "source-thread", name, arguments, "direct")
    assert direct["code"] == "UNKNOWN_GATEWAY"
    gateway = await runner._tool(attempt, "source-thread", "taskhub_call",
        {"operation": name, "arguments": arguments}, "gateway")
    assert gateway["code"] == "UNKNOWN_OPERATION"
    assert await hub.list_artifacts() == []
    assert (await hub.authorize_worker(actor))["state"] == "running"


async def test_core_state_cannot_be_snapshotted_into_itself(active_worker):
    hub, _ = active_worker
    with pytest.raises(RegistryError, match="state directory"):
        snapshot_sources(hub, hub.root)


def test_plugin_exposes_generic_optional_hooks():
    plugin = LeanPlugin()
    names = {tool["name"] for tool in plugin.worker_tools()}
    assert names == {"lean_check", "lean_registry_list"}
    assert not hasattr(plugin, "legacy_worker_tool_names")
    assert all(name not in plugin.worker_instructions() for name in (
        "lean_import_source", "lean_snapshot", "lean_stage_record"))
    assert "no required reasoning order" in plugin.worker_instructions()
    for tool in plugin.worker_tools():
        assert tool["inputSchema"]["additionalProperties"] is False

async def _approved_environment(hub, *, enabled):
    from formal_geyser.registry import ObjectRegistry, file_sha256
    from formal_geyser.schemas import EnvironmentRecord
    # A synthetic pinned toolchain: the tests' Lean service answers for it (FakeRunner); nothing runs it.
    runtime = Path(hub.root) / "toolchain" / "lean"
    runtime.parent.mkdir(exist_ok=True)
    runtime.write_bytes(b"synthetic Lean executable")
    runtime.with_name("lake").write_bytes(b"synthetic Lake executable")
    operator = Actor(role=Role.OPERATOR)
    artifact = await ObjectRegistry(hub).stage(EnvironmentRecord(
        name="Synthetic worker environment", lean_executable=str(runtime),
        lean_executable_sha256=file_sha256(runtime), lean_version="synthetic-version",
        lake_executable_sha256=file_sha256(runtime.with_name("lake")),
        local_execution="trusted-code-only" if enabled else "disabled",
    ), operator)
    return await hub.execute(Command(name="artifact.accept", key=str(uuid4()), payload={
        "artifact_id": artifact["id"], "policy": "lean.environment@1", "verdict": "accepted",
    }), operator)


async def test_worker_development_execution_disabled_by_default(active_worker, lean_service):
    hub, actor = active_worker
    environment = await _approved_environment(hub, enabled=False)
    result = await handle_worker_tool(hub, "lean_check", {
        "environment_artifact": environment["id"], "modules": ["Utility"],
    }, actor, key="check-disabled")
    assert result["verdict"] == "unavailable"
    assert result["proof_accepted"] is False
    assert lean_service.specs == []  # Nothing reached the Lean service.


async def test_worker_development_report_is_immutable_candidate_evidence(active_worker, lean_service):
    hub, actor = active_worker
    environment = await _approved_environment(hub, enabled=True)
    workspace = hub.workspace(actor.worker_id)
    source = b"-- Software metadata fixture only.\n"
    (workspace / "Utility.lean").write_bytes(source)
    result = await handle_worker_tool(hub, "lean_check", {
        "environment_artifact": environment["id"], "modules": ["Utility"],
    }, actor, key="check-fixture")
    assert result["ok"] is True
    assert result["proof_accepted"] is False
    # A development check on the interactive lane reads the workspace in place, with no snapshot.
    [spec] = lean_service.specs
    assert (spec.lane, spec.owner) == ("interactive", actor.worker_id)
    assert spec.request.snapshot is None and Path(spec.request.directory) == workspace.resolve()
    assert result["sources_sha256"] == digest({"Utility.lean": hashlib.sha256(source).hexdigest()})
    assert "snapshot" not in result
    # Development checks confer no acceptance: no kernel replay.
    assert spec.request.kernel_replay is False and lean_service.runner.replays == []
    # Lake built the workspace's file in the service's own slot; no compiled output reaches the workspace.
    [build] = [Path(kwargs["cwd"]) for argv, kwargs in lean_service.runner.calls if "build" in argv]
    assert not build.resolve().is_relative_to(workspace.resolve())
    assert (build / "source" / "Utility.lean").read_bytes() == source
    report = await hub.artifact(result["diagnostic_id"])
    assert report["kind"] == "tool.lean-development"
    assert report["accepted"] is False
    assert report["input_refs"] == [environment["id"]]
    assert report["data"]["sources_sha256"] == result["sources_sha256"] and "snapshot" not in report["data"]
    assert not list(workspace.rglob("*.olean")) and not (workspace / ".lake").exists()


async def test_worker_development_checks_keep_file_work_off_the_event_loop(active_worker, monkeypatch):
    from formal_geyser import worker_tools
    from formal_geyser.lean_service import executor
    hub, actor = active_worker
    environment = await _approved_environment(hub, enabled=True)
    source = f"-- Software metadata fixture only, {uuid4().hex}.\n".encode()
    (hub.workspace(actor.worker_id) / "Utility.lean").write_bytes(source)
    on_loop = []
    # Choosing the source directory in TaskHub, and reading its files in the Lean service.
    for module, name in ((worker_tools, "source_directory"), (executor, "working_sources")):
        monkeypatch.setattr(module, name, lambda *args, original=getattr(module, name), **kwargs: (
            on_loop.append(threading.current_thread() is threading.main_thread()) or original(*args, **kwargs)))
    result = await handle_worker_tool(hub, "lean_check", {
        "environment_artifact": environment["id"], "modules": ["Utility"]}, actor, key="check-off-loop")
    assert result["ok"] is True and on_loop == [False, False]
    # The files are read where they are: no temporary check tree and no stored snapshot of them.
    assert not (Path(hub.state_dir) / "lean-worker-checks").exists()
    assert not hub.blobs.has(hashlib.sha256(source).hexdigest())


async def test_a_development_check_stopped_for_its_memory_tells_the_worker_why(active_worker, monkeypatch):
    # In the Linux pilot a worker given only `KeyError: 'doctor'` ran the same check 21 times.
    from formal_geyser import worker_tools
    from formal_geyser.lean_service import executor
    hub, actor = active_worker
    environment = await _approved_environment(hub, enabled=True)
    (hub.workspace(actor.worker_id) / "Utility.lean").write_bytes(b"-- Software metadata fixture only.\n")
    request = executor.CheckRequest.model_validate({
        "environment": {}, "blobs": "/b", "directory": "/w", "modules": ["Utility"]})

    async def stopped(*args, **kwargs):
        return executor.memory_limited(request, "interactive", 12, 13 * 2**30,
                                       result={"doctor": {"ok": True}, "sources_sha256": "0" * 64})
    monkeypatch.setattr(worker_tools.lean_service, "check", stopped)
    result = await handle_worker_tool(hub, "lean_check", {
        "environment_artifact": environment["id"], "modules": ["Utility"]}, actor, key="check-memory")
    assert result["ok"] is False and result["sources_sha256"] == "0" * 64
    assert result["reason"].startswith("The development check was stopped: one of its Lean processes held 13.0 GiB")


async def test_development_checks_never_read_private_notes(active_worker, lean_service):
    hub, actor = active_worker
    environment = await _approved_environment(hub, enabled=True)
    notes = hub.workspace(actor.worker_id) / ".private" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "Utility.lean").write_text("-- A private note.\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="only a draft directory"):
        await handle_worker_tool(hub, "lean_check", {"directory": ".private/notes",
            "environment_artifact": environment["id"], "modules": ["Utility"]}, actor, key="private-check")
    (notes.parent / "drafts").mkdir()
    with pytest.raises(RegistryError, match="only a draft directory"):  # Drafts, but not one draft.
        await handle_worker_tool(hub, "lean_check", {"directory": ".private/drafts",
            "environment_artifact": environment["id"], "modules": ["Utility"]}, actor, key="drafts-check")
    assert lean_service.specs == []  # Refused before the Lean service read anything.


async def test_a_draft_compiles_over_the_rest_of_the_workspace(active_worker, lean_service):
    hub, actor = active_worker
    environment = await _approved_environment(hub, enabled=True)
    workspace = hub.workspace(actor.worker_id)
    (workspace / "Utility.lean").write_bytes(b"-- A helper the draft imports.\n")
    (workspace / "Work.lean").write_bytes(b"-- The workspace's own version.\n")
    draft = workspace / ".private" / "drafts" / "next"
    draft.mkdir(parents=True)
    (draft / "Work.lean").write_bytes(b"import Utility\n-- The unfinished draft.\n")
    result = await handle_worker_tool(hub, "lean_check", {
        "directory": ".private/drafts/next", "environment_artifact": environment["id"],
        "modules": ["Work"]}, actor, key="check-draft")
    assert result["ok"] is True
    [spec] = lean_service.specs
    assert Path(spec.request.directory) == workspace.resolve() and Path(spec.request.overlay) == draft.resolve()
    # The draft's file takes the place of the workspace's file of the same module.
    [build] = [Path(kwargs["cwd"]) for argv, kwargs in lean_service.runner.calls if "build" in argv]
    assert (build / "source" / "Work.lean").read_bytes() == (draft / "Work.lean").read_bytes()
    assert (build / "source" / "Utility.lean").is_file()

async def test_running_runtime_dispatches_lean_tools_without_second_owner(active_worker):
    from formal_geyser.coordination.api import ProjectOwner
    hub, actor = active_worker
    workspace = hub.workspace(actor.worker_id)
    (workspace / "metadata.txt").write_text("Runtime dispatch fixture.", encoding="utf-8")
    attempt = await hub.authorize_worker(actor)
    with ProjectOwner(hub.root):
        runner = WorkerRunner(hub)
        assert not hasattr(runner, "legacy_domain_names")
        assert "lean_import_source" not in runner.domain_names
        first = await stage_source_file(hub, actor, "metadata.txt", key="call-1", runner=runner)
        repeated = await stage_source_file(hub, actor, "metadata.txt", key="call-1", runner=runner)
        listed = await runner._tool(attempt, "synthetic-thread", "taskhub_call",
            {"operation": "lean_registry_list", "arguments": {"object_type": "statement"}}, "query-1")
    assert first["id"] == repeated["id"]
    assert not first["accepted"]
    assert listed["total"] == 1 and listed["items"][0]["id"] == first["id"]
    assert listed["items"][0]["accepted"] is False


async def test_gateway_file_reads_keep_workspace_scope_and_current_fence(active_worker):
    from formal_geyser.coordination.runtime.runner import WorkerRunner

    hub, actor = active_worker
    (hub.workspace(actor.worker_id) / "draft.md").write_text("Retained draft.", encoding="utf-8")
    attempt = await hub.authorize_worker(actor)
    runner = WorkerRunner(hub)
    assert {tool["name"] for tool in runner.tools} == {"taskhub_actions", "taskhub_call"}
    arguments = {"operation": "workspace_read", "arguments": {"path": "draft.md"}}
    page = await runner._tool(attempt, "current-thread", "taskhub_call", arguments, "read")
    assert page["text"] == "Retained draft."
    with pytest.raises(HubError):
        await runner._tool({**attempt, "fencing_token": attempt["fencing_token"] + 1},
                           "current-thread", "taskhub_call", arguments, "stale-read")
    with pytest.raises(ValueError):
        await runner._tool(attempt, "current-thread", "taskhub_call", {
            "operation": "workspace_read", "arguments": {"path": "../outside"}}, "outside")
