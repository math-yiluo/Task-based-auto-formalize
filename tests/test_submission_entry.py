"""Native submission boundaries with synthetic records and no model or Lean execution."""
from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    Role,
    TaskContract,
)
from formal_geyser.coordination.runtime.runner import WorkerRunner
from formal_geyser.coordination.runtime.tools import operation_tools
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import RegistryError, reference_ids
from formal_geyser.schemas import METADATA_POLICIES, OBJECT_ADAPTER
from tests.conftest import snapshot
from tests.coordination.gateway_helpers import file_arguments
from tests.coordination.publication_helpers import new_hub

# The shape of a retired SourceRecord; current code accepts no such record kind.
RETIRED_SOURCE = {"object_type": "source", "name": "Synthetic source", "media_type": "text/plain",
                  "content_sha256": "0" * 64, "byte_length": 6}


def native_reader(monkeypatch, hub, task, snapshot_sha):
    """Use the real manifest reader, replacing only its worker/transport context."""
    workspace = hub.root / "native-work"
    workspace.mkdir(exist_ok=True)
    attempt = {"id": "attempt-1", "worker_id": "worker-1", "fencing_token": 1,
               "task_id": task["id"], "task_revision": task["revision"]}

    async def authorize(actor):
        assert actor == Actor(role=Role.WORKER, worker_id="worker-1",
                              attempt_id="attempt-1", fencing_token=1)
        return attempt

    async def read_task(task_id, revision=None):
        assert task_id == task["id"] and revision in {None, task["revision"]}
        return deepcopy(task)

    monkeypatch.setattr(hub, "authorize_worker", authorize, raising=False)
    monkeypatch.setattr(hub, "task", read_task, raising=False)
    monkeypatch.setattr(hub, "workspace", lambda worker_id: workspace, raising=False)
    monkeypatch.setattr(hub, "_candidate_snapshot", lambda worker_id: snapshot_sha, raising=False)
    runner = WorkerRunner.__new__(WorkerRunner)
    runner.hub = hub
    runner.plugin = LeanPlugin()
    return runner, attempt, workspace


async def read_manifest(runner, attempt, workspace, payload):
    (workspace / "submission.json").write_text(json.dumps(payload), encoding="utf-8")
    return await runner._submission_arguments(attempt, {"manifest_path": "submission.json"})


def record_data(hub, object_type):
    common = {"name": "Synthetic " + object_type}
    if object_type in {"definition", "statement"}:
        data = {**common, "entry_module": "Candidate", "declaration": "Candidate.fixture",
                "provenance": "internal", "provenance_rationale": "Synthetic test protocol."}
        if object_type == "definition":
            data["description"] = "Synthetic definition metadata."
        else:
            data["statement"] = "Synthetic statement metadata."
        return data
    if object_type == "proof":
        return {**common, "entry_module": "Candidate", "declaration": "Candidate.target",
                "statement_declaration": "Baseline.target"}
    if object_type == "build-candidate":
        return common
    raise AssertionError(object_type)


@pytest.mark.parametrize("object_type", [
    "definition", "statement", "proof", "build-candidate",
])
async def test_native_manifest_normalizes_every_worker_record_kind(
    monkeypatch, proof_case, build_case, object_type,
):
    hub, task, artifact = build_case if object_type == "build-candidate" else proof_case
    if object_type == "statement":
        artifact = {**artifact, "snapshot": snapshot(hub, "Candidate.lean", b"def fixture : Prop := True\n")}
    runner, attempt, workspace = native_reader(monkeypatch, hub, task, artifact["snapshot"])
    payload = {"kind": "lean." + object_type, "data": record_data(hub, object_type),
               "introduction": "Inspect the exact synthetic record and its dependencies.",
               "input_refs": ["environment-1"], "coauthors": ["Worker 1"]}
    before = deepcopy(hub.artifacts)

    result = await read_manifest(runner, attempt, workspace, payload)

    record = OBJECT_ADAPTER.validate_python(result["data"])
    assert record.object_type == object_type
    assert result["policy"] == METADATA_POLICIES.get(object_type, task["contract"]["acceptance_policy"])
    assert set(result["input_refs"]) == {
        "environment-1", *task["contract"]["inputs"], *reference_ids(record),
    }
    assert result["introduction"] == payload["introduction"]
    assert result["coauthors"] == payload["coauthors"]
    assert hub.artifacts == before and hub.commands == []
    assert "accepted" not in result and "evidence" not in result
    if object_type in {"definition", "statement", "proof"}:
        assert result["data"]["bundle"]["snapshot"] == artifact["snapshot"]
        assert result["repository_paths"] == ["Candidate.lean", "Candidate.md"]
        if object_type == "statement":
            assert "specification_artifact" not in result["data"]
            assert hub.blobs.get(record.bundle.lean_sha256) == b"def fixture : Prop := True\n"
    else:
        assert result["repository_paths"] == []


@pytest.mark.parametrize("object_type", ["environment", "source", "statement-binding"])
async def test_native_manifest_rejects_operator_and_unknown_records(
    monkeypatch, proof_case, object_type,
):
    hub, task, artifact = proof_case
    runner, attempt, workspace = native_reader(monkeypatch, hub, task, artifact["snapshot"])
    if object_type == "environment":
        data = deepcopy(hub.artifacts["environment-1"]["data"])
    else:
        data = {"name": "Retired record kind", "object_type": object_type}
    before = deepcopy(hub.artifacts)
    with pytest.raises(ValueError, match="operator authority|object_type|tag"):
        await read_manifest(runner, attempt, workspace, {"kind": "lean." + object_type, "data": data})
    assert hub.artifacts == before and hub.commands == []


@pytest.mark.parametrize("defect", [
    "wrong-policy", "wrong-kind", "pair-hash", "retired-definitions", "retired-fingerprint",
    "retired-statement-binding", "successor-name", "literature-hash", "retired-specification",
])
async def test_native_metadata_manifest_rejects_invalid_policy_and_retired_claims(
    monkeypatch, proof_case, defect,
):
    hub, task, artifact = proof_case
    runner, attempt, workspace = native_reader(monkeypatch, hub, task, artifact["snapshot"])
    payload = {"kind": "lean.statement", "data": record_data(hub, "statement")}
    if defect == "wrong-policy":
        payload["policy"] = "lean.peer@1"
    elif defect == "wrong-kind":
        payload["data"]["object_type"] = "review"
    elif defect == "pair-hash":
        payload["data"]["bundle"] = {"lean_sha256": "0" * 64}
    elif defect == "retired-definitions":
        payload["data"]["definitions"] = [{"artifact_id": "environment-1", "version": 1}]
    elif defect == "retired-fingerprint":
        payload["data"]["target_type_sha256"] = "1" * 64
    elif defect == "retired-statement-binding":
        payload = {"kind": "lean.proof", "data": record_data(hub, "proof")}
        payload["data"].update(statement_artifact="statement-1", statement_version=1)
    elif defect == "successor-name":
        payload["data"].update(supersedes="statement-1", version=2, name="A different identity")
    elif defect == "literature-hash":
        payload["data"].update(provenance="source", sources=[
            {"literature_id": "unregistered", "content_sha256": "0" * 64, "locator": "all"}])
    elif defect == "retired-specification":
        payload["data"]["specification_artifact"] = "specification-1"
    before = deepcopy(hub.artifacts)
    with pytest.raises(ValueError):  # RegistryError or a schema ValidationError.
        await read_manifest(runner, attempt, workspace, payload)
    assert hub.artifacts == before and hub.commands == []


def test_new_catalog_exposes_only_file_submission_boundaries():
    catalog = {tool["name"]: tool for tool in operation_tools() + LeanPlugin().worker_tools()}
    assert "lean_stage_record" not in catalog
    for name in ("taskhub_stage", "taskhub_contribute"):
        schema = catalog[name]["inputSchema"]
        assert set(schema["properties"]) == {"manifest_path"}
        validator = Draft202012Validator(schema)
        assert validator.is_valid({"manifest_path": "submission.json"})
        for invalid in ({}, {"manifest_path": ""}, {"kind": "note", "data": {}},
                        {"manifest_path": "submission.json", "policy": "lean.peer@1"}):
            assert not validator.is_valid(invalid)


@pytest.fixture
async def active_submission_worker(tmp_path):
    from tests.bootstrap_helpers import synthetic_execution

    hub = synthetic_execution(await new_hub(tmp_path, config=ProjectConfig(domain="lean")))
    operator = Actor(role=Role.OPERATOR)

    async def command(name, payload):
        return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload), operator)

    try:
        contract = TaskContract(title="Synthetic native submission", objective="Exercise metadata only.",
                                domain="lean", acceptance_policy="lean.peer@1")
        task = await command("task.publish", {"contract": contract.model_dump(mode="json")})
        run = await command("run.start", {})
        worker = await command("worker.register", {})
        attempt = await command("task.claim", {"task_id": task["id"], "revision": task["revision"],
                                                "worker_id": worker["id"], "run_id": run["id"]})
        yield hub, attempt
    finally:
        await hub.close()


async def test_hidden_legacy_record_alias_cannot_create_content_only_publication(active_submission_worker):
    hub, attempt = active_submission_worker
    runner = WorkerRunner(hub)
    args = {"record": dict(RETIRED_SOURCE)}
    assert "lean_stage_record" not in {tool["name"] for tool in runner.tools}
    before = await hub.list_artifacts()
    for _ in range(2):
        rejected = await runner._tool(attempt, "legacy-thread", "taskhub_call", {
            "operation": "lean_stage_record", "arguments": args}, "same-call")
        assert rejected["code"] == "UNKNOWN_OPERATION"
    assert await hub.list_artifacts() == before
    stale = {**attempt, "fencing_token": attempt["fencing_token"] + 1}
    with pytest.raises(HubError):
        await runner._tool(stale, "legacy-thread", "taskhub_call", {
            "operation": "lean_registry_list", "arguments": {}}, "stale-call")


@pytest.mark.parametrize("name", ["taskhub_stage", "taskhub_contribute"])
async def test_manifest_metadata_alone_cannot_stage_or_drain_a_public_note(active_submission_worker, name):
    hub, attempt = active_submission_worker
    workspace = hub.workspace(attempt["worker_id"])
    payload = {"kind": "research.note", "data": {"summary": "Metadata without an authored note file."},
               "introduction": "Review the synthetic note."}
    (workspace / "submission.json").write_text(json.dumps(payload), encoding="utf-8")
    before = await hub.list_artifacts()
    with pytest.raises(HubError) as rejected:
        await WorkerRunner(hub)._tool(attempt, "file-thread", "taskhub_call",
                                     {"operation": (name).removeprefix("taskhub_"), "arguments": file_arguments(WorkerRunner(hub).hub.workspace(attempt["worker_id"]), (name).removeprefix("taskhub_"), {"manifest_path": "submission.json"})}, "file-call")
    assert rejected.value.code == "SOURCE_FILES_REQUIRED"
    assert await hub.list_artifacts() == before
    await hub.authorize_worker(Actor(role=Role.WORKER, worker_id=attempt["worker_id"],
        attempt_id=attempt["id"], fencing_token=attempt["fencing_token"]))


@pytest.mark.parametrize("name", ["taskhub_stage", "taskhub_contribute"])
async def test_selected_note_files_reach_stage_or_drain_boundary(active_submission_worker, name):
    hub, attempt = active_submission_worker
    workspace = hub.workspace(attempt["worker_id"])
    (workspace / "note.md").write_text("# Synthetic work product\nReview this exact source file.\n", encoding="utf-8")
    payload = {"kind": "research.note", "data": {"summary": "Synthetic source note."},
               "introduction": "Inspect note.md.", "repository_paths": ["note.md"]}
    (workspace / "submission.json").write_text(json.dumps(payload), encoding="utf-8")
    result = await WorkerRunner(hub)._tool(attempt, "file-thread", "taskhub_call",
                                         {"operation": (name).removeprefix("taskhub_"), "arguments": file_arguments(WorkerRunner(hub).hub.workspace(attempt["worker_id"]), (name).removeprefix("taskhub_"), {"manifest_path": "submission.json"})}, "file-call")
    if name == "taskhub_stage":
        candidate = await hub.artifact(result["artifact_id"])
        assert candidate["publication_policy"] == "lean.peer@1"
        assert candidate["accepted"] is False and candidate["category"] == "note"
        assert candidate["review_task_id"]
        assert candidate["repository"]["paths"] == ["note.md"]
    else:
        assert result["runtime_directive"] == "drain_for_submission"


async def test_generic_note_cannot_smuggle_reserved_lean_metadata(active_submission_worker):
    hub, attempt = active_submission_worker
    workspace = hub.workspace(attempt["worker_id"])
    payload = {"kind": "research.note", "policy": "lean.peer@1",
               "data": dict(RETIRED_SOURCE)}
    (workspace / "submission.json").write_text(json.dumps(payload), encoding="utf-8")
    before = await hub.list_artifacts()
    with pytest.raises(RegistryError, match="reserved Lean record|kind"):
        await WorkerRunner(hub)._tool(attempt, "note-thread", "taskhub_call",
                                     {"operation": 'stage', "arguments": file_arguments(WorkerRunner(hub).hub.workspace(attempt["worker_id"]), 'stage', {"manifest_path": "submission.json"})}, "invalid-note")
    assert await hub.list_artifacts() == before


@pytest.mark.parametrize("operation", ["stage", "contribute"])
async def test_retired_manifest_returns_domain_error_without_changing_execution(active_submission_worker, operation):
    hub, attempt = active_submission_worker
    workspace = hub.workspace(attempt["worker_id"])
    (workspace / "retired.json").write_text(json.dumps({
        "kind": "lean.source", "data": dict(RETIRED_SOURCE),
        "repository_paths": ["source.txt"], "introduction": "A retired source-record route must be rejected."}), encoding="utf-8")
    (workspace / "source.txt").write_text("Retained source acquisition fixture.", encoding="utf-8")
    runner = WorkerRunner(hub)
    before = await hub.list_artifacts()
    with pytest.raises(ValueError, match="source"):
        await runner._tool(attempt, "fixture-thread", "taskhub_call", {
            "operation": operation, "arguments": {"manifest_path": "retired.json"}}, "invalid-manifest")
    assert await hub.list_artifacts() == before
    assert (await hub.run(attempt["run_id"]))["state"] == "running"
    assert (await hub.authorize_worker(runner._worker_actor(attempt)))["state"] == "running"
    read = await runner._tool(attempt, "fixture-thread", "taskhub_call", {
        "operation": "workspace_read", "arguments": {"path": "source.txt"}}, "continue-after-error")
    assert read["text"] == "Retained source acquisition fixture."
