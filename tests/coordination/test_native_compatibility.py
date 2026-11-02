"""File-first gateway requests retain fences and receipts without native models."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import (
    ScriptedAction,
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.actions import ActionCatalog
from formal_geyser.coordination.runtime.tools import (
    WORKER_INSTRUCTIONS,
    dynamic_tools,
    operation_tools,
)
from formal_geyser.coordination.storage import canonical
from formal_geyser.plugin import LeanPlugin


def test_fresh_catalog_uses_file_boundaries_and_stable_native_entrypoints():
    assert {item["name"] for item in dynamic_tools()} == {"taskhub_actions", "taskhub_call"}
    actions = ActionCatalog(operation_tools() + LeanPlugin().worker_tools())
    names = {item["operation"] for item in actions.describe()["operations"]}
    assert {"read", "lean_import_source", "lean_snapshot", "lean_stage_record"}.isdisjoint(names)
    assert {"lean_check", "lean_registry_list", "fetch"} <= names
    assert "material_sha256" not in WORKER_INSTRUCTIONS
    for name in ("publish", "propose_revision"):
        schema = actions.describe(operation=name)["inputSchema"]
        Draft202012Validator.check_schema(schema)
        assert "contract" not in schema["properties"]
        assert "contract_path" in schema["required"]
        extras = ({"task_id": "task", "revision": 1, "reason": "Clarify the method."}
                  if name == "propose_revision" else {})
        validator = Draft202012Validator(schema)
        assert validator.is_valid({**extras, "contract_path": "tasks/proposal.json"})
        for payload in (extras, {**extras, "contract_path": ""},
                        {**extras, "contract": {"title": "Old inline", "objective": "Inspect."}},
                        {**extras, "contract_path": "task.json", "contract": {}}):
            assert not validator.is_valid(payload)


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex),
                             Actor(role=Role.OPERATOR))


@pytest.fixture
async def active_worker(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    task = await command(hub, "task.publish", {"contract": {
        "title": "File boundaries", "objective": "Keep exact files, authority, and receipts."}})
    run = await command(hub, "run.start")
    attempt = await hub.next_assignment(run["id"])
    try:
        yield hub, task, run, attempt
    finally:
        await hub.close()


async def call(runner, attempt, operation, arguments, call_id):
    return await runner._tool(attempt, "gateway-thread", "taskhub_call",
                              {"operation": operation, "arguments": arguments}, call_id)


async def test_file_task_publication_and_revision_keep_receipts_and_revision_checks(active_worker):
    hub, _, _, attempt = active_worker
    runner = WorkerRunner(hub, ScriptedHarness)
    contract = {"title": "Exact file task", "objective": "Read exact files."}
    path = hub.workspace(attempt["worker_id"]) / "task.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    first = await call(runner, attempt, "publish", {"contract_path": "task.json"}, "publish")
    repeated = await call(runner, attempt, "publish", {"contract_path": "task.json"}, "publish")
    assert first["id"] == repeated["id"]
    updated_contract = {**contract, "context": "An additional relevant file is available."}
    path.write_text(json.dumps(updated_contract), encoding="utf-8")
    revision = {"task_id": first["id"], "revision": first["revision"], "reason": "Supply context.",
                "contract_path": "task.json"}
    result = await call(runner, attempt, "propose_revision", revision, "revise")
    assert result == await call(runner, attempt, "propose_revision", revision, "revise")
    assert (await hub.task(first["id"]))["contract"]["context"] == updated_contract["context"]
    with pytest.raises(HubError):
        await call(runner, attempt, "propose_revision", revision, "stale-revision")


async def test_retired_aliases_and_blob_reads_are_rejected_and_file_reads_stay_fenced(active_worker):
    hub, _, _, attempt = active_worker
    runner = WorkerRunner(hub, ScriptedHarness)
    for name in ("task_publish", "taskhub_publish", "taskhub_private", "taskhub_read"):
        rejected = await runner._tool(attempt, "gateway-thread", name, {}, "old-" + name)
        assert rejected["code"] == "UNKNOWN_GATEWAY"
    for operation in ("private", "read", "lean_snapshot", "note_submit"):
        assert (await call(runner, attempt, operation, {}, operation))["code"] == "UNKNOWN_OPERATION"
    assert await hub.list_artifacts() == []
    workspace = hub.workspace(attempt["worker_id"])
    (workspace / "draft.md").write_text("Exact private file", encoding="utf-8")
    page = await call(runner, attempt, "workspace_read", {"path": "draft.md"}, "read")
    assert page["text"] == "Exact private file" and not page["has_more"]
    stale = {**attempt, "fencing_token": attempt["fencing_token"] + 1}
    with pytest.raises(HubError):
        await call(runner, stale, "workspace_read", {"path": "draft.md"}, "stale-read")
    with pytest.raises(HubError):
        await runner._tool(stale, "gateway-thread", "taskhub_actions", {}, "stale-discovery")


async def test_completion_cannot_be_used_as_submission_and_rejection_does_not_drain(active_worker):
    hub, task, _, attempt = active_worker
    runner = WorkerRunner(hub, ScriptedHarness)
    payloads = [{"kind": "json", "data": {"candidate": "An old inline submission."}},
                {"manifest_path": "candidate.json"}]
    receipt_count = await hub.db.call(lambda c: c.execute("SELECT count(*) FROM receipts").fetchone()[0])
    for index, arguments in enumerate(payloads):
        call_id = f"submit-{index}"
        responses = []
        for _ in range(2):
            rejected = await call(runner, attempt, "complete", arguments, call_id)
            assert rejected["code"] == "INVALID_ARGUMENTS"
            responses.append(rejected)
        assert responses[0] == responses[1]
        native_receipt = await hub.db.call(lambda c: dict(c.execute(
            "SELECT * FROM native_call_receipts WHERE worker_id=? AND thread_id=? AND call_id=?",
            (attempt["worker_id"], "gateway-thread", call_id),
        ).fetchone()))
        assert native_receipt["command_json"] is None
        assert native_receipt["core_response_json"] is None
        assert json.loads(native_receipt["response_json"]) == responses[0]
        receipt_key = "native-tool:" + hashlib.sha256(canonical({
            "worker_id": attempt["worker_id"], "thread_id": "gateway-thread", "call_id": call_id,
        }).encode("utf-8")).hexdigest()
        assert await hub.db.call(lambda c: c.execute(
            "SELECT count(*) FROM receipts WHERE key=?", (receipt_key,),
        ).fetchone()[0]) == 0
    assert await hub.db.call(lambda c: c.execute("SELECT count(*) FROM receipts").fetchone()[0]) == receipt_count
    assert await hub.list_artifacts() == []
    await hub.authorize_worker(runner._worker_actor(attempt))
    assert (await hub.worker(attempt["worker_id"]))["state"] != "draining"
    assert (await hub.task(task["id"]))["state"] != "done"


async def test_manifest_without_real_files_is_rejected_before_staging(active_worker):
    hub, _, _, attempt = active_worker
    runner = WorkerRunner(hub, ScriptedHarness)
    manifest = hub.workspace(attempt["worker_id"]) / "candidate.json"
    manifest.write_text(json.dumps({"kind": "json", "data": {"candidate": "Inline only"},
                                    "introduction": "Inspect the candidate."}), encoding="utf-8")
    with pytest.raises(HubError) as rejected:
        await call(runner, attempt, "stage", {"manifest_path": "candidate.json"}, "missing-files")
    assert rejected.value.code == "SOURCE_FILES_REQUIRED"
    assert await hub.list_artifacts() == []
    await hub.authorize_worker(runner._worker_actor(attempt))


async def test_gateway_telemetry_keeps_operation_arguments_and_native_tool_names(active_worker):
    hub, task, run, attempt = active_worker
    harness = ScriptedHarness([ScriptedTurn([
        ScriptedAction("taskhub_actions", {"operation": "inspect"}),
        ScriptedAction("taskhub_call", {"operation": "inspect", "arguments": {"task_id": task["id"]}}),
    ])])
    runner = WorkerRunner(hub, lambda: harness)
    await runner.run_once(run["id"], assignment=attempt)
    worker = await hub.worker(attempt["worker_id"])
    page = runner.transcripts.session_page(worker["id"], "thread:" + worker["thread_id"])
    actions = page["turns"][0]["actions"]
    assert [item["tool"] for item in actions] == ["taskhub_actions", "taskhub_call"]
    assert [item["arguments"]["operation"] for item in actions] == ["inspect", "inspect"]
    assert actions[1]["arguments"]["arguments"] == {"task_id": task["id"]}
