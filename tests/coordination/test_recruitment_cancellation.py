"""Obsolete helper cancellation retains process and accounting evidence."""
from __future__ import annotations

import asyncio
import json

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    TurnOutcome,
    WorkerRunner,
)
from tests.coordination.test_native_runtime import command
from tests.coordination.test_native_runtime import native_runtime as native_runtime

RUNNER = Actor(role=Role.RUNNER)


async def recruitment(native_runtime):
    case = await native_runtime()
    first = ScriptedHarness([ScriptedTurn([])])
    source = await WorkerRunner(case.hub, lambda: first).run_once(case.run["id"])
    intent = (await case.hub.due_recruitment_intents(case.run["id"]))[0]
    return case, source, intent


async def leave(case, source, intent):
    return await command(case.hub, "task.leave", {"task_id": intent["source_task_id"],
        "worker_id": source["worker_id"], "permanent": True,
        "reason": "The source executor permanently left this synthetic task."})


@pytest.mark.parametrize("outcome", ["interrupted", "unknown", "usage_error"])
async def test_obsolete_running_helper_interrupts_and_preserves_uncertain_outcomes(native_runtime, outcome):
    case, source, intent = await recruitment(native_runtime)
    started, stopped = asyncio.Event(), asyncio.Event()

    class HeldHelper(ScriptedHarness):
        interrupted = False

        async def interrupt(self):
            self.interrupted = True
            stopped.set()

        async def run_turn(self, prompt, *, on_tool, on_usage, on_started):
            await on_started("synthetic-helper-turn")
            started.set()
            await stopped.wait()
            sample = {"totalTokens": 7}
            if outcome != "usage_error":
                sample["lastTokenUsage"] = {"totalTokens": 7}
            await on_usage(sample)
            self.process_quiet = outcome != "unknown"
            output = json.dumps({"decision": True, "invitation": "This result is obsolete."})
            return TurnOutcome("synthetic-helper-turn", outcome, outcome != "unknown", self.process_quiet,
                               visible_output=output, final_output=output)

    helper = HeldHelper()
    running = asyncio.create_task(WorkerRunner(case.hub, lambda: helper).run_helper(intent))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await leave(case, source, intent)
        if outcome == "interrupted":
            result = await asyncio.wait_for(running, timeout=5)
            assert result["directive"] == "helper_cancelled"
        else:
            with pytest.raises(HubError):
                await asyncio.wait_for(running, timeout=5)
        assert helper.interrupted
        retained = await case.hub.db.call(lambda c: case.hub._helper(c, intent["id"]))
        lifecycle = (await case.hub.task(intent["source_task_id"]))["collaboration"]["recruitment"]
        assert lifecycle["opportunity_state"] == "cancelled"
        assert retained["raw_result_json"] is None
        assert retained["state"] == ("cancelled" if outcome == "interrupted" else "uncertain")
        assert retained["inference_uncertain"] == (0 if outcome == "interrupted" else 1)
        run = await case.hub.run(case.run["id"])
        assert run["usage_uncertain"] == (0 if outcome == "interrupted" else 1)
        assert run["state"] == "running"
        usage = await case.hub.db.call(lambda c: c.execute(
            "SELECT COALESCE(sum(delta_tokens),0) FROM helper_usage_samples WHERE helper_id=?", (intent["id"],)).fetchone()[0])
        assert usage == (0 if outcome == "usage_error" else 7)
        newcomer = await command(case.hub, "worker.register")
        with pytest.raises(HubError):
            await command(case.hub, "task.claim", {"task_id": intent["source_task_id"], "revision": 1,
                "worker_id": newcomer["id"], "run_id": case.run["id"], "plan": "Try the obsolete opening."})
        assert (await case.hub.worker(newcomer["id"]))["active_attempt"] is None
    finally:
        stopped.set()
        if not running.done():
            await asyncio.gather(running, return_exceptions=True)


async def test_cancellation_during_session_preparation_never_dispatches_inference(native_runtime, monkeypatch):
    case, source, intent = await recruitment(native_runtime)

    class NeverRun(ScriptedHarness):
        async def run_turn(self, *args, **kwargs):
            raise AssertionError("The permanently cancelled opportunity must not start inference")

    helper = NeverRun()
    runner = WorkerRunner(case.hub, lambda: helper)
    check = runner._helper_cancellation

    async def cancel_at_preflight(retained):
        await leave(case, source, intent)
        return await check(retained)

    monkeypatch.setattr(runner, "_helper_cancellation", cancel_at_preflight)
    result = await runner.run_helper(intent)
    assert result["directive"] == "helper_cancelled" and helper.process_quiet
    retained = await case.hub.db.call(lambda c: case.hub._helper(c, intent["id"]))
    assert retained["state"] == "cancelled" and retained["remote_turn_id"] is None
    proof = await case.hub.db.call(lambda c: json.loads(c.execute(
        "SELECT payload_json FROM events WHERE kind='helper.cancelled' AND object_id=?", (intent["id"],)).fetchone()[0]))
    assert proof["inference_dispatched"] is False and proof["terminal_observed"] is False


@pytest.mark.parametrize("case_name", ["no_quiet", "unknown_terminal", "missing_dispatch_proof", "bound_but_not_dispatched",
                                      "uncertain", "live_opportunity", "wrong_actor"])
async def test_cancelled_helper_requires_explicit_safe_boundary(native_runtime, case_name):
    case, source, intent = await recruitment(native_runtime)
    await command(case.hub, "helper.started", {"intent_id": intent["id"], "thread_id": "synthetic-helper"}, RUNNER)
    if case_name == "bound_but_not_dispatched":
        await command(case.hub, "helper.turn.bind", {"intent_id": intent["id"], "remote_turn_id": "bound-turn"}, RUNNER)
    if case_name != "live_opportunity":
        await leave(case, source, intent)
    if case_name == "uncertain":
        await case.hub.helper_uncertain(intent["id"], "Unknown helper execution")
    payload = {"intent_id": intent["id"], "process_quiet": True, "terminal_observed": True,
               "inference_dispatched": True, "reason": "Synthetic cancellation boundary"}
    if case_name == "no_quiet":
        payload["process_quiet"] = False
    elif case_name == "unknown_terminal":
        payload["terminal_observed"] = False
    elif case_name == "missing_dispatch_proof":
        payload.pop("inference_dispatched")
    elif case_name == "bound_but_not_dispatched":
        payload.update(inference_dispatched=False, terminal_observed=False)
    actor = Actor(role=Role.OPERATOR) if case_name == "wrong_actor" else RUNNER
    with pytest.raises(HubError):
        await command(case.hub, "helper.cancelled", payload, actor)
    retained = await case.hub.db.call(lambda c: case.hub._helper(c, intent["id"]))
    assert retained["state"] == ("uncertain" if case_name == "uncertain" else "running")
