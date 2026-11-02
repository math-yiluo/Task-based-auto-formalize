"""Decisions of the cheap decision model that fail, are paused, outlive their run or go stale,
and what each one sees and may use."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime import helpers as decision_helpers
from formal_geyser.coordination.runtime.codex import (
    CodexHarness,
    DecisionHarness,
    TurnOutcome,
)
from formal_geyser.coordination.runtime.handoff import handoff_path
from formal_geyser.coordination.runtime.helpers import decision_schema
from formal_geyser.coordination.runtime.model_view import _notification_summary
from tests.coordination.publication_helpers import new_hub
from tests.coordination.test_assignment_decisions import Recording, idle_after, publish

OPERATOR, RUNNER = Actor(role=Role.OPERATOR), Actor(role=Role.RUNNER)


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


@pytest.fixture
async def project(tmp_path):
    now = [1_800_000_000_000]
    hub = await new_hub(tmp_path / "project", clock=lambda: now[0])
    Recording.opened = []
    try:
        yield SimpleNamespace(hub=hub, now=now)
    finally:
        await hub.close()


class Hanging(ScriptedHarness):
    """A decision turn that ends only when interrupted, then with `outcome`."""

    def __init__(self, answer=None):
        super().__init__([])
        self.answer = answer
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.config = SimpleNamespace(stop_grace_seconds=1)

    async def run_turn(self, prompt, *, on_tool, on_usage, on_started):
        self.prompts.append(prompt)
        turn_id = "turn-" + uuid4().hex
        await on_started(turn_id)
        self.started.set()
        await self.release.wait()
        self.process_quiet = True
        if self.answer is None:
            return TurnOutcome(turn_id, "interrupted", True, True)
        text = json.dumps(self.answer)
        return TurnOutcome(turn_id, "completed", True, True, visible_output=text, final_output=text)

    async def interrupt(self):
        self.release.set()


async def first_turn_done(hub, run, title="Large task"):
    """A task's first executor that has just finished its first turn."""
    await publish(hub, title)
    attempt = await hub.next_assignment(run["id"])
    await command(hub, "worker.bind_session", {"worker_id": attempt["worker_id"], "thread_id": "first-thread",
                  "codex_profile": hub.config.codex.model_dump(mode="json")}, RUNNER)
    await hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    return attempt


async def test_a_recruitment_decision_that_keeps_failing_defaults_to_no(project):
    hub = project.hub
    run = await command(hub, "run.start")
    attempt = await first_turn_done(hub, run)
    failing = WorkerRunner(hub, lambda: ScriptedHarness([ScriptedTurn([], status="failed")]))
    for _ in range(2):
        [intent] = await hub.due_recruitment_intents(run["id"])
        assert await hub.next_assignment(run["id"]) is None  # The executor waits for its decision.
        with pytest.raises(HubError):
            await failing.run_helper(intent)
        project.now[0] += 31_000
        await hub.recover_quiet_runtime_failures(run["id"])
    # The decision model failed twice: no collaborator, and the executor continues.
    assert await hub.due_recruitment_intents(run["id"]) == []
    resumed = await hub.next_assignment(run["id"])
    assert (resumed["worker_id"], resumed["task_id"]) == (attempt["worker_id"], attempt["task_id"])
    recruitment = (await hub.task(attempt["task_id"]))["collaboration"]["recruitment"]
    assert recruitment["recruitment_decision"] == "no"
    assert [event["payload"] for event in await hub.events(limit=500)
            if event["kind"] == "recruitment.defaulted"] == [{"reason": "decision_failed"}]


@pytest.mark.parametrize("kind", ["assignment", "recruitment"])
async def test_a_decision_past_its_time_limit_is_stopped_and_its_default_decides(project, monkeypatch, kind):
    hub = project.hub
    monkeypatch.setattr(decision_helpers, "DECISION_SECONDS", 0.3)
    run = await command(hub, "run.start")
    if kind == "assignment":
        idle = await idle_after(hub, run, "Earlier task")
        task = await publish(hub, "Next task")
        assert await hub.next_assignment(run["id"]) is None
        [intent] = await hub.due_assignment_intents(run["id"])
    else:
        attempt = await first_turn_done(hub, run)
        [intent] = await hub.due_recruitment_intents(run["id"])
    harness = Hanging()
    result = await WorkerRunner(hub, lambda: harness).run_helper(intent)
    assert harness.release.is_set()  # The time limit interrupted the turn.
    stored = await hub.db.call(lambda c: c.execute(
        "SELECT raw_result_json FROM helper_intents WHERE id=?", (intent["id"],)).fetchone()[0])
    assert json.loads(stored)["timed_out"] is True
    [event] = [event["payload"] for event in await hub.events(limit=500) if event["kind"] == kind + ".decided"]
    assert event["timed_out"] is True and event["invalid"] is False
    if kind == "assignment":  # None suitable: the task gets a new worker.
        assert result["outcome"] == "new_worker" and result["assignment"]["task_id"] == task["id"]
        assert result["assignment"]["worker_id"] != idle["id"]
    else:  # No collaborator.
        assert result["directive"] == "recruitment_decided"
        assert (await hub.task(attempt["task_id"]))["collaboration"]["recruitment"]["recruitment_decision"] == "no"


async def test_a_paused_assignment_decision_is_asked_again_and_is_no_failure(project):
    hub = project.hub
    run = await command(hub, "run.start")
    idle = await idle_after(hub, run, "Earlier task")
    task = await publish(hub, "Next task")
    for _ in range(2):
        assert await hub.next_assignment(run["id"]) is None
        [intent] = await hub.due_assignment_intents(run["id"])
        harness = Hanging()
        helper = asyncio.create_task(WorkerRunner(hub, lambda: harness).run_helper(intent))
        await harness.started.wait()
        await command(hub, "run.pause", {"run_id": run["id"]})
        assert (await helper)["reason"] == "run_control"
        await command(hub, "run.resume", {"run_id": run["id"]})
        await hub.finish_dispatch_release()
    assert await hub.db.call(lambda c: hub._failed_decisions(c, task["id"])) == 0
    # The task is still decided on, not given to a new worker for want of a decision.
    assert await hub.next_assignment(run["id"]) is None
    [intent] = await hub.due_assignment_intents(run["id"])
    assert idle["id"] in {item["worker_id"] for item in intent["candidates"]["workers"]}


async def waiting_for_a_decision_under_pause_all(hub, group):
    """Pause all with `group` resumed, an idle Codex worker, and a task waiting for its decision."""
    run = await command(hub, "run.start")
    idle = await idle_after(hub, run, "Earlier task")
    await command(hub, "run.pause", {"run_id": run["id"]})
    await command(hub, "group.resume", {"group": group})
    task = await publish(hub, "Next task")
    assert await hub.next_assignment(run["id"]) is None
    [intent] = await hub.due_assignment_intents(run["id"])
    return run, idle, task, intent


async def test_an_assignment_decision_runs_while_a_group_of_workers_is_resumed_under_pause_all(project):
    hub = project.hub
    run, idle, task, intent = await waiting_for_a_decision_under_pause_all(hub, "codex.workers")
    harness = ScriptedHarness([ScriptedTurn([], visible_output=json.dumps({"worker": idle["display_name"]}))])
    result = await WorkerRunner(hub, lambda: harness).run_helper(intent)
    assert len(harness.prompts) == 1 and result["directive"] == "assigned"
    assert result["outcome"] == "idle_worker" and result["worker_id"] == idle["id"]
    assert result["assignment"]["task_id"] == task["id"]
    assert (await hub.run(run["id"]))["run_state"] == "paused"


async def test_a_group_of_reviewers_alone_runs_no_assignment_decision_under_pause_all(project):
    hub = project.hub
    run = await command(hub, "run.start")
    await idle_after(hub, run, "Earlier task")
    await publish(hub, "Next task")
    assert await hub.next_assignment(run["id"]) is None
    [intent] = await hub.due_assignment_intents(run["id"])
    await command(hub, "run.pause", {"run_id": run["id"]})
    await command(hub, "group.resume", {"group": "codex.reviewers"})
    runner = WorkerRunner(hub, ScriptedHarness)
    assert await hub.due_assignment_intents(run["id"]) == []
    assert await runner._helper_may_run(intent) is False
    await command(hub, "group.resume", {"group": "claude"})
    assert await runner._helper_may_run(intent) is True


async def test_a_failed_assignment_decision_recovers_while_a_group_of_workers_is_resumed_under_pause_all(project):
    hub = project.hub
    run, idle, task, intent = await waiting_for_a_decision_under_pause_all(hub, "codex.workers")
    with pytest.raises(HubError):
        await WorkerRunner(hub, lambda: ScriptedHarness([ScriptedTurn([], status="failed")])).run_helper(intent)
    stuck = await hub.db.call(lambda c: hub._helper(c, intent["id"]))
    assert stuck["state"] == "uncertain"
    project.now[0] += 31_000
    await hub.recover_quiet_runtime_failures(run["id"])
    assert (await hub.db.call(lambda c: hub._helper(c, intent["id"])))["state"] == "cancelled"
    assert await hub.next_assignment(run["id"]) is None
    [again] = await hub.due_assignment_intents(run["id"])  # Its task is decided on again.
    assert again["id"] != intent["id"] and again["source_task_id"] == task["id"]


async def test_an_unfinished_assignment_decision_continues_in_the_next_run(project):
    hub = project.hub
    first_run = await command(hub, "run.start")
    await idle_after(hub, first_run, "Earlier task")
    await publish(hub, "A")
    assert await hub.next_assignment(first_run["id"]) is None
    [intent] = await hub.due_assignment_intents(first_run["id"])
    harness = Hanging({"worker": None})
    helper = asyncio.create_task(WorkerRunner(hub, lambda: harness).run_helper(intent))
    await harness.started.wait()
    await command(hub, "run.pause_after_turn", {"run_id": first_run["id"]})
    harness.release.set()
    assert (await helper)["directive"] == "helper_deferred"  # Its answer is kept for later.
    await command(hub, "run.stop", {"run_id": first_run["id"]})
    second_run = await command(hub, "run.start")
    [kept] = await hub.due_assignment_intents(second_run["id"])
    assert kept["id"] == intent["id"]
    committed = await WorkerRunner(hub, ScriptedHarness).run_helper(kept)
    assert committed["outcome"] == "new_worker"
    later = await publish(hub, "B")
    assert await hub.next_assignment(second_run["id"]) is None  # B's own decision, not a deadlock.
    assert [item["source_task_id"] for item in await hub.due_assignment_intents(second_run["id"])] == [later["id"]]


async def test_a_decision_whose_task_changed_makes_no_model_call(project):
    hub = project.hub
    run = await command(hub, "run.start")
    idle = await idle_after(hub, run, "Earlier task")
    task = await publish(hub, "Next task")
    assert await hub.next_assignment(run["id"]) is None
    [intent] = await hub.due_assignment_intents(run["id"])
    other = await command(hub, "worker.register")
    await command(hub, "task.claim", {"task_id": task["id"], "revision": task["revision"],
                                      "run_id": run["id"], "worker_id": other["id"]})
    harness = ScriptedHarness([ScriptedTurn([], visible_output=json.dumps({"worker": idle["display_name"]}))])
    result = await WorkerRunner(hub, lambda: harness).run_helper(intent)
    assert harness.prompts == [] and result["outcome"] == "opportunity_changed"


async def test_a_decision_runs_without_native_tools_over_an_empty_directory(project, monkeypatch):
    # Command lines only: no Codex needs to be installed.
    monkeypatch.setattr("formal_geyser.coordination.runtime.codex.resolve_codex_executable",
                        lambda executable: "/usr/bin/codex")
    hub = project.hub
    run = await command(hub, "run.start")
    idle = await idle_after(hub, run, "Earlier task")
    await publish(hub, "Next task")
    assert await hub.next_assignment(run["id"]) is None
    [intent] = await hub.due_assignment_intents(run["id"])
    assert decision_schema(intent)["properties"]["worker"]["enum"] == [idle["display_name"], None]

    def recording():
        return Recording([ScriptedTurn([], visible_output=json.dumps({"worker": None}))])
    await WorkerRunner(hub, recording).run_helper(intent)
    [opened] = Recording.opened
    assert opened["workspace"] == hub.state_dir / "decision-workspace" and opened["read_only"] is True
    assert list(opened["workspace"].iterdir()) == []
    command_line = DecisionHarness.launch_command(hub.config.codex)
    for feature in ("shell_tool", "computer_use", "image_generation"):
        assert command_line[command_line.index(feature) - 1] == "--disable"
    # No conversation sees the user's own skills.
    for harness in (CodexHarness, DecisionHarness):
        line = harness.launch_command(hub.config.codex)
        assert line[line.index("skip_host_skill_discovery") - 1] == "--enable"
    assert "shell_tool" not in CodexHarness.launch_command(hub.config.codex)


async def test_a_recruitment_decision_reads_the_executors_handoff(project):
    hub = project.hub
    run = await command(hub, "run.start")
    attempt = await first_turn_done(hub, run)
    path = hub.workspace(attempt["worker_id"]) / handoff_path(attempt["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Goal\n\nThe large task.\n\n## Current state\n\nThe definitions are done.\n\n"
                    "## Next step\n\nProve the two independent lemmas.\n", encoding="utf-8")
    [intent] = await hub.due_recruitment_intents(run["id"])
    harness = ScriptedHarness([ScriptedTurn([], visible_output=json.dumps({"decision": False}))])
    await WorkerRunner(hub, lambda: harness).run_helper(intent)
    entry = json.loads(harness.prompts[0])
    assert entry["handoff"] == {"Current state": "The definitions are done.",
                                "Next step": "Prove the two independent lemmas."}
    summary, _ = _notification_summary("recruitment.decided", {"decision": False, "applied": True})
    assert summary == "recruitment decided: decision no"


async def test_a_new_worker_chosen_while_its_account_is_held_starts_once_it_is_free(project, monkeypatch):
    from formal_geyser.coordination.runtime import claude as claude_runtime
    monkeypatch.setattr(claude_runtime, "claude_unavailable", lambda config: None)
    hub = project.hub
    run = await command(hub, "run.start")
    idle = await idle_after(hub, run, "Earlier task")
    await command(hub, "runtime.select_backend", {"backend": "claude"})
    task = await publish(hub, "Next task")
    assert await hub.next_assignment(run["id"]) is None
    [intent] = await hub.due_assignment_intents(run["id"])
    assert await hub.hold_dispatch("claudeUsageLimit", "Limit reached", subject_id="turn") is True
    harness = ScriptedHarness([ScriptedTurn([], visible_output=json.dumps({"worker": None}))])
    result = await WorkerRunner(hub, lambda: harness).run_helper(intent)
    assert result["outcome"] == "new_worker_held"
    # Nothing is asked again while the account is held, and no worker starts on it.
    assert await hub.next_assignment(run["id"]) is None
    assert await hub.due_assignment_intents(run["id"]) == []
    await command(hub, "run.resume", {"run_id": run["id"]})
    await hub.finish_dispatch_release()
    assigned = await hub.next_assignment(run["id"])
    assert assigned["task_id"] == task["id"] and assigned["worker_id"] != idle["id"]
    assert len(harness.prompts) == 1
