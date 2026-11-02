"""Transport control stays backstage while native tool replies retain task data."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from formal_geyser.coordination.models import CodexConfig
from formal_geyser.coordination.runtime.codex import (
    CodexHarness,
    SessionInfo,
    ToolResult,
)


@pytest.mark.parametrize("directive", [
    "drain_for_wait", "drain_for_submission", "drain_for_release", "drain_for_review",
])
async def test_tool_reply_consumes_control_without_exposing_it_or_rewriting_authored_data(directive):
    harness = CodexHarness(CodexConfig(), command=[])
    harness.session = SessionInfo("native-thread", "in-memory", "test")
    harness._turn_terminal = asyncio.get_running_loop().create_future()
    acknowledgments = []
    calls = []

    async def acknowledged():
        acknowledgments.append("delivered")

    authored = {"runtime_directive": "An example field in the supplied input",
                "call_id": "an authored domain reference", "candidate_hash": "b" * 64}
    result = ToolResult({"runtime_directive": directive, "task_id": "task-review",
                         "revision": 7, "candidate_hash": "a" * 64, "payload": authored},
                        after_send=acknowledged)
    original = deepcopy(dict(result))

    async def tool(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return result

    harness._on_tool = tool
    reply = await harness._request("item/tool/call", {
        "threadId": "native-thread", "turnId": "native-turn", "callId": "native-call",
        "tool": "taskhub_call", "arguments": {"operation": "review", "arguments": {}},
    })
    assert calls == [("taskhub_call", {"operation": "review", "arguments": {}}, "native-call")]
    assert harness._directive == directive and not harness._drain.is_set()
    assert acknowledgments == [] and reply.result["success"] is True
    text = reply.result["contentItems"][0]["text"]
    assert json.loads(text) == {key: value for key, value in original.items() if key != "runtime_directive"}
    assert all(value not in text for value in ("native-thread", "native-turn", "native-call", directive))
    assert result == original and result.after_send is acknowledged
    assert reply.after_send is not None
    await reply.after_send()
    assert acknowledgments == ["delivered"] and harness._drain.is_set()
    harness._turn_terminal.set_result("interrupted")


async def test_native_file_reply_omits_transport_hashes_but_keeps_business_references():
    harness = CodexHarness(CodexConfig(), command=[])
    harness.session = SessionInfo("native-thread", "in-memory", "test")
    harness._turn_terminal = asyncio.get_running_loop().create_future()
    original = {"material_path": ".private/materials/answer.json", "requires_read": True,
                "material_sha256": "transport-hash", "material_bytes": 29000,
                "candidate_hash": "business-hash", "artifact_id": "candidate"}

    async def tool(name, arguments, call_id):
        return original

    harness._on_tool = tool
    reply = await harness._request("item/tool/call", {
        "threadId": "native-thread", "turnId": "native-turn", "callId": "native-call",
        "tool": "taskhub_call", "arguments": {"operation": "fetch", "arguments": {}},
    })
    visible = json.loads(reply.result["contentItems"][0]["text"])
    assert visible == {key: value for key, value in original.items()
                       if key not in {"material_sha256", "material_bytes"}}
    assert original["material_sha256"] == "transport-hash"
    harness._turn_terminal.set_result("completed")
