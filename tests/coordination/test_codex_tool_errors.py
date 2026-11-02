from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest

from formal_geyser.coordination.errors import HubError, WorkerWorkspaceError
from formal_geyser.coordination.models import CodexConfig
from formal_geyser.coordination.runtime.codex import CodexHarness, SessionInfo
from formal_geyser.coordination.runtime.rpc import JsonRpcClient


class MemoryRpc:
    """Drive the real dynamic-tool adapter without a process or model turn."""

    def __init__(self, harness):
        self.harness = harness
        self.reply = None
        self.closed = False

    async def request(self, method, params, **kwargs):
        assert method == "turn/start"
        assert params["threadId"] == "test-thread"
        self.reply = await self.harness._request("item/tool/call", {
            "threadId": "test-thread", "turnId": "test-turn", "callId": "test-call",
            "tool": "taskhub_call", "arguments": {"operation": "dag_update", "arguments": {}},
        })
        assert self.reply.after_send is None
        await self.harness._notification("turn/completed", {
            "threadId": "test-thread", "turn": {"id": "test-turn", "status": "completed"},
        })
        return {"turn": {"id": "test-turn"}}

    async def wait_failure(self):
        await asyncio.Event().wait()

    async def drain_callbacks(self, **kwargs):
        pass

    async def finish_receiving(self, **kwargs):
        pass

    async def close(self):
        self.closed = True


async def tool_failure(error):
    harness = CodexHarness(CodexConfig(), command=[])
    harness.session = SessionInfo("test-thread", "in-memory", "test")
    rpc = MemoryRpc(harness)
    harness.rpc = cast(JsonRpcClient, rpc)
    started = []

    async def on_tool(name, arguments, call_id):
        assert (name, arguments, call_id) == (
            "taskhub_call", {"operation": "dag_update", "arguments": {}}, "test-call")
        raise error

    async def on_usage(sample):
        pytest.fail("The in-memory adapter test must not produce model usage")

    async def on_started(turn_id):
        started.append(turn_id)

    outcome = await harness.run_turn(
        "Exercise tool failure", on_tool=on_tool, on_usage=on_usage, on_started=on_started)
    assert started == ["test-turn"]
    assert outcome.status == "completed" and outcome.error is None
    assert outcome.terminal_observed and outcome.process_quiet and rpc.closed
    assert rpc.reply is not None and rpc.reply.result["success"] is False
    content = rpc.reply.result["contentItems"]
    assert len(content) == 1 and content[0]["type"] == "inputText"
    return json.loads(content[0]["text"]), rpc.reply.result


def graph_reference():
    root = "/project/shared/dags/revision-4-" + "a" * 64
    return {
        "kind": "shared-dags", "status": "planning", "revision": 4,
        "content_key": "a" * 64, "manifest_sha256": "b" * 64,
        "root": root + "/files", "manifest_path": root + "/manifest.json",
        "definitions": root + "/files/definitions.json", "proofs": root + "/files/proofs.json",
        "current_path": "/project/shared/dags/current.json",
    }


def test_hub_error_optional_details_keep_existing_shape_and_status():
    error = HubError("INVALID_INPUT", "Supply a revision", 422)
    assert error.as_dict() == {"code": "INVALID_INPUT", "message": "Supply a revision"}
    assert error.status == 422 and str(error) == "Supply a revision"
    details = {"current_revision": 4, "current_graph_ref": graph_reference()}
    detailed = HubError("DAG_REVISION_CONFLICT", "Reread both files", details=details)
    assert detailed.as_dict() == {
        "code": "DAG_REVISION_CONFLICT", "message": "Reread both files", "details": details}
    assert HubError("EMPTY", "No detail", details={}).as_dict()["details"] == {}


@pytest.mark.parametrize("error", [
    HubError("INVALID_DAG", "definitions.json has a cycle", 422),
    WorkerWorkspaceError("worker", "task", "The workspace is unavailable"),
])
async def test_adapter_preserves_hub_error_codes_without_details(error):
    payload, _ = await tool_failure(error)
    assert payload == {"error": error.as_dict()}


@pytest.mark.parametrize("details", [
    {"current_revision": 4, "current_graph_ref": graph_reference()},
    {"current_revision": None, "current_graph_ref": None},
])
async def test_adapter_preserves_revision_conflict_recovery_reference(details):
    error = HubError("DAG_REVISION_CONFLICT", "Reread both files and merge your edits", details=details)
    payload, _ = await tool_failure(error)
    assert payload == {"error": error.as_dict()}


async def test_adapter_bounds_entire_reply_and_preserves_recovery_before_diagnostics():
    class Unsafe:
        def __repr__(self):
            raise AssertionError("Error details must not stringify arbitrary objects")

    recursive = {}
    recursive["self"] = recursive
    details = {
        "unsafe": Unsafe(), "nan": float("nan"), "recursive": recursive,
        "surrogate": "\ud800", "bad_key": {"\ud800": "invalid Unicode"},
        "oversized": "x" * 100000, "oversized_number": 10 ** 10000,
        **{f"diagnostic_{index}": "\x00" * 2000 for index in range(80)},
        "current_revision": 4, "current_graph_ref": graph_reference(),
    }
    error = HubError("DAG_REVISION_CONFLICT", "\x00" * 100000, details=details)
    payload, reply = await tool_failure(error)
    structured = payload["error"]
    assert structured["code"] == "DAG_REVISION_CONFLICT"
    assert structured["message"].endswith(" [truncated]")
    assert structured["details_truncated"] is True
    assert structured["details"] == {
        "current_revision": 4, "current_graph_ref": graph_reference()}
    assert len(json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 8192


async def test_adapter_keeps_generic_exception_shape():
    payload, _ = await tool_failure(RuntimeError("A generic failure"))
    assert payload == {"error": "RuntimeError: A generic failure"}
