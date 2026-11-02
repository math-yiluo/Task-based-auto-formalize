"""Once machine validation accepts its candidate, each open review gets its own new
reviewer at once, which starts a new conversation; an assignment decision creates no
worker conversation."""
from __future__ import annotations

import json

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.runtime import (
    ScriptedAction,
    ScriptedHarness,
    ScriptedTurn,
    TurnOutcome,
    WorkerRunner,
)
from tests.coordination.lifecycle_helpers import assignment_decision, validate_candidate
from tests.coordination.publication_helpers import FileHarness, new_hub, publish_batch
from tests.coordination.test_collaborative_runtime import call, contract, review_arguments
from tests.coordination.test_dynamic_lifecycle import actor, command


@pytest.mark.parametrize("accept", [False, True])
async def test_fresh_review_starts_once_without_source_and_retires(tmp_path, accept):
    clock = [10_000]
    hub = await new_hub(tmp_path, clock=lambda: clock[0])
    try:
        await command(hub, "task.publish", {"contract": contract()})
        run = await command(hub, "run.start")
        author = FileHarness([ScriptedTurn([ScriptedAction("taskhub_call", {
            "operation": "contribute", "arguments": {"kind": "json", "data": {"ready": True},
                "repository_paths": ["result.json"]},
        })])], files={"result.json": b'{"ready":true}\n'})
        submitted = await WorkerRunner(hub, lambda: author).run_once(run["id"])
        assert len(await hub.list_workers()) == 1
        review = next(t for t in await hub.list_tasks() if t["contract"]["kind"] == "review")
        # No waiting period: the open review is dispatched at the next admission.
        assignment = await hub.next_assignment(run["id"])
        assert assignment["task_id"] == review["id"]
        reviewers = [w for w in await hub.list_workers() if w["kind"] == "review"]
        assert len(reviewers) == 1
        assert reviewers[0]["display_name"] == "Reviewer 1"
        assert reviewers[0]["worker_number"] is None
        assert len(await hub.list_workers()) == 2
        # The reserved review has exactly one executor, even with another poll.
        assert await hub.next_assignment(run["id"]) is None

        class Reviewer(ScriptedHarness):
            async def run_turn(self, prompt, *, on_started, on_tool, **kwargs):
                packet = json.loads(prompt)
                assert packet["task"]["kind"] == "review"
                assert "Reviewer 1" in prompt
                await on_started("fresh-review-turn")
                binding = (await hub.task(review["id"]))["collaboration"]["review"]
                report = review_arguments(binding)
                if accept:
                    await call(on_tool, "review", report)
                    await self.close()
                    return TurnOutcome("fresh-review-turn", "completed", True, True)
                else:
                    result = await call(on_tool, "review", {
                        **report, "verdict": "reject", "suggestions": ["Correct the result."],
                    })
                await self.close()
                return TurnOutcome("fresh-review-turn", "interrupted", True, True,
                                   result["runtime_directive"])

        harnesses = []

        def factory():
            harness = Reviewer()
            harnesses.append(harness)
            return harness

        runner = WorkerRunner(hub, factory)
        result = await runner.run_once(run["id"], assignment=assignment)
        if accept:
            await publish_batch(hub, run["id"])
        assert len(harnesses) == 1
        assert harnesses[0].opened_threads == [None]  # The review starts a new conversation.
        assert result["worker_id"] == reviewers[0]["id"]
        assert (await hub.worker(result["worker_id"]))["state"] == "retired"
        candidate = await hub.artifact(submitted["artifact_id"])
        assert candidate["status"] == ("accepted" if accept else "rejected")
        assert (await hub.run(run["id"]))["turns_used"] == 2
        ordinary = await command(hub, "worker.register")
        assert ordinary["display_name"] == "Worker 1"
    finally:
        await hub.close()


async def test_fresh_reviews_start_at_once_without_cooldown_and_exclude_the_author(tmp_path):
    from tests.coordination.publication_helpers import file_payload

    clock = [10_000]
    hub = await new_hub(tmp_path, clock=lambda: clock[0])
    try:
        await command(hub, "task.publish", {"contract": contract()})
        run = await command(hub, "run.start")
        author = await hub.next_assignment(run["id"])
        candidates = []
        for index in range(2):
            candidates.append(await command(hub, "artifact.stage", file_payload(
                hub, author, data={"index": index}), actor(author)))
            await validate_candidate(hub, candidates[-1]["artifact_id"])
        first = await hub.next_assignment(run["id"])
        second = await hub.next_assignment(run["id"])
        assert {first["task_id"], second["task_id"]} == {item["review_task_id"] for item in candidates}
        assert first["worker_id"] != author["worker_id"] and second["worker_id"] != author["worker_id"]
        assert await hub.next_assignment(run["id"]) is None
        assert len(await hub.list_workers()) == 3
        assert [w["display_name"] for w in await hub.list_workers() if w["kind"] == "review"] == [
            "Reviewer 1", "Reviewer 2"]
    finally:
        await hub.close()


async def test_reviews_start_only_after_machine_validation_accepts_the_exact_candidate(tmp_path):
    from tests.coordination.publication_helpers import file_payload

    hub = await new_hub(tmp_path)
    try:
        await command(hub, "task.publish", {"contract": contract()})
        run = await command(hub, "run.start")
        author = await hub.next_assignment(run["id"])
        staged = [await command(hub, "artifact.stage", file_payload(hub, author, data={"index": index}),
                                actor(author)) for index in range(3)]
        # Staged without a validation verdict: no reviewer starts, and none can be admitted.
        assert await hub.next_assignment(run["id"]) is None
        spare = await hub.db.call(lambda c: hub._new_worker(c, kind="review"), write=True)
        with pytest.raises(HubError) as pending:
            await command(hub, "task.claim", {"task_id": staged[2]["review_task_id"], "revision": 1,
                                              "worker_id": spare, "run_id": run["id"]})
        assert pending.value.code == "VALIDATION_PENDING"
        # An unavailable check is no receipt, and a machine rejection never gets a reviewer.
        await validate_candidate(hub, staged[0]["artifact_id"], verdict="unavailable")
        await validate_candidate(hub, staged[1]["artifact_id"], verdict="rejected")
        assert await hub.next_assignment(run["id"]) is None
        # The retried check accepts: that review starts at once, in its own new conversation.
        await validate_candidate(hub, staged[0]["artifact_id"])
        first = await hub.next_assignment(run["id"])
        assert first["task_id"] == staged[0]["review_task_id"]
        assert first["worker_id"] != spare
        assert await hub.next_assignment(run["id"]) is None
        await validate_candidate(hub, staged[2]["artifact_id"])
        second = await hub.next_assignment(run["id"])
        assert second["task_id"] == staged[2]["review_task_id"] and second["worker_id"] != spare
        assert await hub.next_assignment(run["id"]) is None
        assert (await hub.task(staged[1]["review_task_id"]))["collaboration"]["members"] == []
    finally:
        await hub.close()


async def test_first_decision_starts_directly_without_creating_a_worker_conversation(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        task = await command(hub, "task.publish", {"contract": contract()})
        run = await command(hub, "run.start")
        source = await command(hub, "worker.register")  # A freshly registered worker is idle.
        intent = await assignment_decision(hub, run["id"])
        assert [item["worker_id"] for item in intent["candidates"]["workers"]] == [source["id"]]
        harnesses = []

        def factory():
            result = ScriptedHarness([ScriptedTurn([], visible_output=json.dumps({
                "worker": source["display_name"]}))])
            harnesses.append(result)
            return result

        decided = await WorkerRunner(hub, factory).run_helper(intent)
        assert decided["outcome"] == "idle_worker" and decided["assignment"]["task_id"] == task["id"]
        assert len(harnesses) == 1
        assert harnesses[0].opened_threads == [None]
        assert len(harnesses[0].prompts) == 1
        # The chosen worker starts the task in a new conversation of its own when it runs.
        assert (await hub.worker(source["id"]))["thread_id"] is None
        assert [w["id"] for w in await hub.list_workers() if w["kind"] == "ordinary"] == [source["id"]]
    finally:
        await hub.close()
