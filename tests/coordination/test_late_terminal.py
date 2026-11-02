from __future__ import annotations

import asyncio

import pytest

from formal_geyser.coordination.models import CodexConfig
from formal_geyser.coordination.runtime.codex import CodexHarness, SessionInfo


class DelayedTerminalRpc:
    async def request(self, method, params, **kwargs):
        return {"turn": {"id": "exact-native-turn"}} if method == "turn/start" else {}

    async def wait_failure(self):
        await asyncio.Future()


@pytest.mark.parametrize("terminal_status", ["interrupted", "completed", None])
async def test_late_terminal_is_retained_only_after_successful_close(terminal_status):
    usage = []

    class Harness(CodexHarness):
        async def _finish_close(self):
            await self._on_usage({"totalTokens": 7})
            if terminal_status is not None:
                self._turn_terminal.set_result(terminal_status)
            self._quiet = True

    harness = Harness(CodexConfig(stop_grace_seconds=0.02), command=["unused"])
    harness.rpc = DelayedTerminalRpc()
    harness.session = SessionInfo("existing-thread", "test", "config")
    harness._quiet = False
    harness._drain.set()
    harness._directive = "drain_for_wait"

    async def unused(*args):
        return {}

    async def record(sample):
        usage.append(sample)

    outcome = await harness.run_turn("Continue.", on_tool=unused, on_usage=record, on_started=unused)
    assert outcome.status == (terminal_status or "unknown")
    assert outcome.turn_id == "exact-native-turn" and outcome.process_quiet
    assert outcome.terminal_observed is (terminal_status is not None)
    assert outcome.directive == "drain_for_wait" and usage == [{"totalTokens": 7}]


async def test_terminal_without_successful_callback_drain_is_not_promoted():
    class Harness(CodexHarness):
        async def _finish_close(self):
            self._turn_terminal.set_result("completed")
            self._quiet = True
            raise OSError("Final usage callback could not be persisted")

    harness = Harness(CodexConfig(stop_grace_seconds=0.02), command=["unused"])
    harness.rpc = DelayedTerminalRpc()
    harness.session = SessionInfo("existing-thread", "test", "config")
    harness._drain.set()

    async def unused(*args):
        return {}

    with pytest.raises(OSError, match="usage callback"):
        await harness.run_turn("Continue.", on_tool=unused, on_usage=unused, on_started=unused)
