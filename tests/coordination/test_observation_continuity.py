"""Recording a result must not change whether a worker can continue."""
from __future__ import annotations

import asyncio

import pytest

from formal_geyser.coordination.models import CodexConfig
from formal_geyser.coordination.runtime import (
    ScriptedAction,
    ScriptedHarness,
    ScriptedTurn,
    ToolResult,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.codex import CodexHarness, SessionInfo
from tests.coordination.test_runtime_failure_isolation import runtime as runtime


@pytest.mark.parametrize("directive", [None, "drain_for_wait"])
async def test_delivered_tool_reply_survives_acknowledgement_recording_failure(directive):
    harness = CodexHarness(CodexConfig(), command=["unused-model-free-fixture"])
    harness.session = SessionInfo("same-thread", "scripted", "fixture-config")
    harness._turn_terminal = asyncio.get_running_loop().create_future()

    async def acknowledge():
        raise OSError("Synthetic receipt-storage failure")

    async def tool(*_):
        return ToolResult({"saved": True, **({"runtime_directive": directive} if directive else {})}, after_send=acknowledge)

    harness._on_tool = tool
    response = await harness._request("item/tool/call", {"threadId": "same-thread", "turnId": "same-turn",
        "callId": "one-call", "tool": "taskhub_call", "arguments": {}})
    assert response.after_send is not None
    await response.after_send()
    assert harness.active_turn_id == "same-turn" and not harness._turn_terminal.done()
    assert harness._drain.is_set() is bool(directive)
    assert "receipt-storage" in harness._last_error


async def test_usage_write_failure_does_not_interrupt_native_completion(runtime, monkeypatch):
    hub, run = runtime
    harness = ScriptedHarness([ScriptedTurn([], usage={"totalTokens": 10})])
    runner = WorkerRunner(hub, lambda: harness)
    original = runner._command

    async def fail_usage(name, *args, **kwargs):
        if name == "usage.record":
            raise OSError("Synthetic ledger temporarily unavailable")
        return await original(name, *args, **kwargs)

    monkeypatch.setattr(runner, "_command", fail_usage)
    result = await runner.run_once(run["id"])
    assert result["turn_status"] == "completed"
    assert (await hub.worker(result["worker_id"]))["work_intention"] == "working"
    assert (await hub.run(run["id"]))["usage_uncertain"]


async def test_tool_observation_failure_preserves_the_operation_result(runtime, monkeypatch):
    hub, run = runtime
    harness = ScriptedHarness([ScriptedTurn([ScriptedAction("taskhub_call", {"operation": "checkpoint", "arguments": {
        "checkpoint": {"summary": "Useful progress", "next_action": "Continue"}}})])])
    runner = WorkerRunner(hub, lambda: harness)

    def broken_observation(*args, **kwargs):
        raise OSError("Synthetic dashboard history write failure")

    monkeypatch.setattr(runner.transcripts, "record_action", broken_observation)
    result = await runner.run_once(run["id"])
    assert result["turn_status"] == "completed"
    assert harness.results[0]["checkpoint"]["summary"] == "Useful progress"
