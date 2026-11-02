"""Conversations are replaced by new ones that continue the same task: after idle time, or
in the aggressive mode once the context is large, starting from the handoff and state files."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

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
from formal_geyser.coordination.runtime.handoff import handoff_path
from tests.coordination.lifecycle_helpers import idle_worker, record_saved_turn, resolve_recruitment

OPERATOR, RUNNER = Actor(role=Role.OPERATOR), Actor(role=Role.RUNNER)
MINUTE = 60_000
LARGE = {"totalTokens": 130_000, "lastTokenUsage": {"inputTokens": 125_000, "cachedInputTokens": 100_000,
                                                     "totalTokens": 125_500}}
SMALL = {"totalTokens": 21_000, "lastTokenUsage": {"inputTokens": 20_000, "cachedInputTokens": 15_000,
                                                   "totalTokens": 20_500}}


async def grown_conversation(hub, run):
    """A conversation that began at 20k tokens of context and has grown past the limit."""
    first, _ = await activate(hub, run, ScriptedTurn([], usage=SMALL))
    _, harness = await activate(hub, run, ScriptedTurn([ScriptedAction("taskhub_actions", {})], usage=LARGE))
    return first, harness


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    now = [1_800_000_000_000]
    hub.clock = lambda: now[0]
    await command(hub, "task.publish", {"contract": {"title": "Valuation bound", "objective": "Prove the bound."}})
    run = await command(hub, "run.start")
    try:
        yield hub, run, now
    finally:
        await hub.close()


def write_handoff(hub, worker_id, task_id, next_step="Stage the pair."):
    target = hub.workspace(worker_id) / handoff_path(task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"## Goal\n\nProve the bound.\n\n## Current state\n\nFirst case done.\n\n"
                      f"## Next step\n\n{next_step}\n", encoding="utf-8")


async def activate(hub, run, turn=None):
    """Run the next admitted activation with one scripted turn; return its harness."""
    await resolve_recruitment(hub, run["id"])  # The first turn's collaboration decision: no.
    assignment = await hub.next_assignment(run["id"])
    harness = ScriptedHarness([turn or ScriptedTurn([])])
    await WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=assignment)
    return assignment, harness


async def rotations(hub):
    return await hub.db.call(lambda c: [dict(row) for row in c.execute(
        "SELECT * FROM rotations ORDER BY requested_at,id")])


async def test_the_rotation_mode_is_global_with_a_per_worker_override(project):
    hub, _, _ = project
    assert await hub.rotation_settings() == {"mode": "conservative", "source": "config",
                                             "idle_minutes": 40, "context_tokens": 100_000,
                                             "idle_by_backend": {"codex": {"minutes": 40, "source": "config"},
                                                                 "claude": {"minutes": 40, "source": "config"}}}
    chosen = await command(hub, "runtime.set_rotation_mode", {"mode": "aggressive"})
    assert chosen["mode"] == "aggressive" and chosen["source"] == "operator"
    assert (await hub.backend_status())["rotation"]["mode"] == "aggressive"
    worker = await command(hub, "worker.register")
    assert (await hub.worker(worker["id"]))["effective_rotation_mode"] == "aggressive"
    override = await command(hub, "worker.set_rotation_mode", {"worker_id": worker["display_name"], "mode": "conservative"})
    assert override == {"worker_id": worker["id"], "rotation_mode": "conservative", "effective_mode": "conservative"}
    cleared = await command(hub, "worker.set_rotation_mode", {"worker_id": worker["id"], "mode": None})
    assert cleared["effective_mode"] == "aggressive"
    for name, payload in (("runtime.set_rotation_mode", {"mode": "sometimes"}),
                          ("worker.set_rotation_mode", {"worker_id": worker["id"], "mode": "always"})):
        with pytest.raises(HubError):
            await command(hub, name, payload)
    with pytest.raises(HubError):
        await command(hub, "runtime.set_rotation_mode", {"mode": "aggressive"}, RUNNER)


async def test_a_conversation_idle_past_the_limit_continues_in_a_new_one(project):
    hub, run, now = project
    first, harness = await activate(hub, run)
    worker_id, task_id = first["worker_id"], first["task_id"]
    old_thread = (await hub.worker(worker_id))["thread_id"]
    write_handoff(hub, worker_id, task_id)
    now[0] += 39 * MINUTE
    _, resumed = await activate(hub, run)
    assert resumed.opened_threads == [old_thread]  # Not yet idle long enough.
    now[0] += 41 * MINUTE
    _, rotated = await activate(hub, run)
    assert rotated.opened_threads == [None]
    prompt = json.loads(rotated.prompts[0])
    assert prompt["rotation"]["reason"] == "idle" and prompt["rotation"]["mode"] == "conservative"
    assert prompt["rotation"]["handoff"] == handoff_path(task_id)
    assert prompt["rotation"]["state"] == f".private/tasks/{task_id}/state.md"
    assert prompt["instruction"].startswith("This task continues from an earlier conversation of yours")
    state = (hub.workspace(worker_id) / prompt["rotation"]["state"]).read_text(encoding="utf-8")
    assert state.startswith("# State of task: Valuation bound") and "the conversation had been idle" in state
    [rotation] = await rotations(hub)
    assert (rotation["state"], rotation["reason"], rotation["new_thread_id"]) == (
        "completed", "idle", rotated.session.thread_id)
    modes = await hub.db.call(lambda c: {row[0] for row in c.execute("SELECT rotation_mode FROM turn_intents")})
    assert modes == {"conservative"}


async def test_each_backend_has_its_own_idle_time_and_zero_turns_idle_rotation_off(project):
    hub, run, now = project
    first, _ = await activate(hub, run)
    worker_id, task_id = first["worker_id"], first["task_id"]
    backend = (await hub.worker(worker_id))["backend"]
    other = "claude" if backend == "codex" else "codex"
    old_thread = (await hub.worker(worker_id))["thread_id"]
    write_handoff(hub, worker_id, task_id)
    settings = await command(hub, "runtime.set_rotation_idle", {"backend": backend, "minutes": 0})
    assert settings["idle_by_backend"][backend] == {"minutes": 0, "source": "operator"}
    assert settings["idle_by_backend"][other] == {"minutes": 40, "source": "config"}
    await command(hub, "runtime.set_rotation_idle", {"backend": other, "minutes": 10})  # Not this worker's.
    now[0] += 12 * 60 * MINUTE
    _, resumed = await activate(hub, run)
    assert resumed.opened_threads == [old_thread]  # Idle rotation is off for its backend.
    await command(hub, "runtime.set_rotation_idle", {"backend": backend, "minutes": 720})
    now[0] += 11 * 60 * MINUTE
    _, resumed = await activate(hub, run)
    assert resumed.opened_threads == [old_thread]
    # Null returns to the project's 40 minutes.
    assert (await command(hub, "runtime.set_rotation_idle", {"backend": backend, "minutes": None})
            )["idle_by_backend"][backend]["source"] == "config"
    now[0] += 41 * MINUTE
    _, rotated = await activate(hub, run)
    assert rotated.opened_threads == [None]
    assert (await hub.backend_status())["rotation"]["idle_by_backend"][other]["minutes"] == 10
    for payload in ({"backend": backend, "minutes": -1}, {"backend": backend, "minutes": 7 * 24 * 60 + 1},
                    {"backend": backend, "minutes": "40"}, {"backend": backend, "minutes": True},
                    {"backend": "gpt", "minutes": 40}):
        with pytest.raises(HubError):
            await command(hub, "runtime.set_rotation_idle", payload)
    with pytest.raises(HubError):
        await command(hub, "runtime.set_rotation_idle", {"backend": backend, "minutes": 40}, RUNNER)


async def test_aggressive_mode_reminds_then_rotates_once_the_handoff_is_ready(project):
    hub, run, _ = project
    await command(hub, "runtime.set_rotation_mode", {"mode": "aggressive"})
    first, harness = await grown_conversation(hub, run)
    worker_id, task_id = first["worker_id"], first["task_id"]
    notice = harness.results[0]["rotation_notice"]
    assert "past 100000 tokens" in notice and handoff_path(task_id) in notice
    thread = (await hub.worker(worker_id))["thread_id"]
    [requested] = await rotations(hub)
    assert (requested["state"], requested["repairs"], requested["context_tokens"]) == ("requested", 1, 125_000)
    # The handoff file still held only its template: one more turn in the same conversation
    # asks to fix it.
    write_handoff(hub, worker_id, task_id)
    _, repair = await activate(hub, run)
    assert repair.opened_threads == [thread]
    instruction = json.loads(repair.prompts[0])["rotation"]["instruction"]
    assert ("is not ready: The handoff file was not updated after the context passed the limit; "
            "Section 'Current state' is empty; Section 'Next step' is empty.") in instruction
    _, rotated = await activate(hub, run)
    assert rotated.opened_threads == [None]
    prompt = json.loads(rotated.prompts[0])["rotation"]
    assert prompt["reason"] == "context" and prompt["handoff"] == handoff_path(task_id)
    [done] = await rotations(hub)
    assert (done["state"], done["automatic"]) == ("completed", 0)
    summary = await hub.rotation_summary()
    [turns] = [item for item in summary["turns"] if item["mode"] == "aggressive"]
    # The 20k turn and the 125k turn: (5k + 25k) / 2 uncached, (20k + 125k) / 2 largest.
    assert turns["first_request_uncached"] == 15_000 and turns["largest_context"] == 72_500
    assert {"reason": "context", "mode": "aggressive", "state": "completed", "automatic": 0, "backend": "codex",
            "count": 1} in summary["rotations"]



async def test_a_rotation_is_ready_before_its_worker_is_free_again(project, monkeypatch):
    # Judged after the command that frees the worker, the next activation could start first and
    # run a turn on the conversation past its limit (a settle waited ten minutes in the pilot).
    hub, run, _ = project
    await command(hub, "runtime.set_rotation_mode", {"mode": "aggressive"})
    first, _ = await grown_conversation(hub, run)
    worker_id, task_id = first["worker_id"], first["task_id"]
    thread = (await hub.worker(worker_id))["thread_id"]
    assert (await hub.requested_rotation(worker_id))["thread_id"] == thread
    assert await hub.requested_rotation(worker_id, "another-conversation") is None
    write_handoff(hub, worker_id, task_id)
    completed, seen = hub.ordinary_turn_completed, []

    async def observed(*args, **kwargs):
        seen.append([row["state"] for row in await rotations(hub)])
        return await completed(*args, **kwargs)
    monkeypatch.setattr(hub, "ordinary_turn_completed", observed)
    await activate(hub, run)  # The repair turn.
    assert seen == [["ready"]]


async def test_a_new_conversation_on_a_task_with_a_written_handoff_starts_from_it(project):
    hub, run, _ = project
    first, _ = await activate(hub, run)
    write_handoff(hub, first["worker_id"], first["task_id"], next_step="Check the carried-over pair.")
    # The conversation is gone (released, or never existed for work carried over from another
    # project); the handoff file is what remains.
    await hub.release_conversation(first["worker_id"], "operator")
    _, again = await activate(hub, run)
    assert again.opened_threads == [None]
    prompt = json.loads(again.prompts[0])
    assert prompt["rotation"]["reason"] == "handoff"
    assert prompt["rotation"]["handoff"] == handoff_path(first["task_id"])
    assert "earlier conversation of yours" in prompt["instruction"]
    state = (hub.workspace(first["worker_id"]) / prompt["rotation"]["state"]).read_text(encoding="utf-8")
    assert "an earlier conversation ended and left your handoff file" in state
    assert await rotations(hub) == []  # Nothing rotated: the new conversation starts from the file.


async def test_without_a_usable_handoff_the_state_file_alone_starts_the_new_conversation(project):
    hub, run, _ = project
    await command(hub, "runtime.set_rotation_mode", {"mode": "aggressive"})
    first, _ = await grown_conversation(hub, run)
    # The worker deleted the handoff file it started with, and the repair turn leaves it missing.
    (hub.workspace(first["worker_id"]) / handoff_path(first["task_id"])).unlink()
    await activate(hub, run)
    _, rotated = await activate(hub, run)
    assert rotated.opened_threads == [None]
    prompt = json.loads(rotated.prompts[0])["rotation"]
    assert prompt["handoff"] == handoff_path(first["task_id"])
    assert prompt["handoff_problems"] == ["The handoff file does not exist"]
    assert "needs attention" in json.loads(rotated.prompts[0])["instruction"]
    state = (hub.workspace(first["worker_id"]) / prompt["state"]).read_text(encoding="utf-8")
    assert (f"**Your handoff file `{handoff_path(first['task_id'])}` needs attention:** "
            "The handoff file does not exist.") in state
    [done] = await rotations(hub)
    assert (done["state"], done["automatic"]) == ("completed", 1)


async def test_a_conversation_that_starts_large_does_not_rotate_at_once(project):
    hub, run, _ = project
    await command(hub, "runtime.set_rotation_mode", {"mode": "aggressive"})
    # Its first request is already past the limit: it has not grown 50k since it began.
    _, harness = await activate(hub, run, ScriptedTurn([ScriptedAction("taskhub_actions", {})], usage=LARGE))
    assert "rotation_notice" not in harness.results[0] and await rotations(hub) == []


async def test_a_new_conversation_is_not_sent_the_updates_its_worker_already_had(project):
    hub, run, now = project
    first, harness = await activate(hub, run)
    worker_id, task_id = first["worker_id"], first["task_id"]
    paths = json.loads(harness.prompts[0])["paths"]
    assert (paths["handoff"], paths["journal"]) == (handoff_path(task_id), ".private/notes/journal.md")

    def delivered(c):
        return c.execute("SELECT MAX(sequence) FROM deliveries WHERE worker_id=? AND state='delivered'",
                         (worker_id,)).fetchone()[0]
    before = await hub.db.call(delivered)
    assert before
    write_handoff(hub, worker_id, task_id)
    now[0] += 41 * MINUTE
    _, rotated = await activate(hub, run)
    assert rotated.opened_threads == [None]

    def first_packet_of_new_conversation(c):
        epoch = hub._get(c, "workers", worker_id)["context_epoch"]
        row = c.execute("SELECT body_sha FROM deliveries WHERE worker_id=? AND context_epoch=? "
                        "AND sequence>0 ORDER BY created_at,rowid", (worker_id, epoch)).fetchall()[-1]
        return hub.blobs.get_json(row["body_sha"])
    packet = await hub.db.call(first_packet_of_new_conversation)
    assert packet["after_sequence"] >= before
    assert all(update["sequence"] > before for update in packet.get("updates", []))


async def test_a_conversation_begun_under_other_instructions_continues_in_a_new_one(project):
    hub, run, _ = project
    first, harness = await activate(hub, run)
    worker_id = first["worker_id"]
    assert (await hub.worker(worker_id))["instructions_sha256"]
    _, resumed = await activate(hub, run)
    assert resumed.opened_threads == [harness.session.thread_id]  # The same instructions: no rotation.
    # Opening an older project marks the conversations it started as begun under other instructions.
    await hub.db.call(lambda c: c.execute("UPDATE workers SET instructions_sha256='expired' WHERE id=?",
                                          (worker_id,)), write=True)
    write_handoff(hub, worker_id, first["task_id"])
    _, rotated = await activate(hub, run)
    assert rotated.opened_threads == [None]
    assert json.loads(rotated.prompts[0])["rotation"]["reason"] == "instructions"
    [done] = await rotations(hub)
    assert (done["reason"], done["state"]) == ("instructions", "completed")


async def test_a_message_for_a_worker_that_left_its_task_does_not_rotate(project):
    hub, run, now = project
    first, _ = await activate(hub, run)
    now[0] += 120 * MINUTE

    def left(c):
        c.execute("UPDATE workers SET current_task_id=NULL WHERE id=?", (first["worker_id"],))
    await hub.db.call(left, write=True)
    attempt = {"task_id": first["task_id"], "execution_mode": "message_only"}
    assert await hub.rotation_due(first["worker_id"], attempt) is None


async def test_a_conservative_worker_is_not_reminded_and_a_parked_or_unsettled_thread_does_not_rotate(project):
    hub, run, now = project
    first, harness = await activate(hub, run, ScriptedTurn([ScriptedAction("taskhub_actions", {})], usage=LARGE))
    assert "rotation_notice" not in harness.results[0] and await rotations(hub) == []
    now[0] += 60 * MINUTE
    worker_id = first["worker_id"]
    assert await hub.rotation_due(worker_id, {"task_id": first["task_id"], "interruption_id": "parked"}) is None
    await hub.db.call(lambda c: c.execute(
        "INSERT INTO message_delivery_holds VALUES('human','delivery-1',?,?,NULL,NULL,'uncertain',NULL,?)",
        (worker_id, (hub._get(c, "workers", worker_id))["thread_id"], now[0])), write=True)
    assert await hub.rotation_due(worker_id, {"task_id": first["task_id"]}) is None
    assert await rotations(hub) == []


async def test_an_unsettled_delivery_keeps_an_idle_worker_out_of_assignment(project):
    hub, run, _ = project
    await activate(hub, run)
    idle = await idle_worker(hub, run, "Earlier task")
    await hub.db.call(lambda c: c.execute(
        "INSERT INTO message_delivery_holds VALUES('peer','delivery-2',?,?,NULL,NULL,'uncertain',NULL,1)",
        (idle["id"], idle["thread_id"])), write=True)
    task = await command(hub, "task.publish", {"contract": {"title": "Next task", "objective": "Work."}})
    assignment = await hub.next_assignment(run["id"])
    assert assignment["task_id"] == task["id"] and assignment["worker_id"] != idle["id"]


async def test_history_search_reads_earlier_conversations_only(project):
    hub, run, _ = project
    first, _ = await activate(hub, run)
    runner = WorkerRunner(hub, ScriptedHarness)
    runner.transcripts._append(first["worker_id"], {"type": "assistant", "thread_id": "older-thread",
                                                    "text": "The padic valuation lemma needs Nat.find."})
    search = ScriptedAction("taskhub_call", {"operation": "history_search", "arguments": {"query": "padic valuation"}})
    _, harness = await activate(hub, run, ScriptedTurn([search]))
    [match] = harness.results[0]["matches"]
    assert match["conversation"] == "older-thread" and "Nat.find" in match["excerpt"]


async def test_each_integration_batch_starts_in_a_new_conversation(tmp_path):
    from formal_geyser.coordination.batch_integration import BATCH_DELAY_MS
    from tests.coordination.publication_helpers import new_hub
    from tests.coordination.test_collaborative_core import quiet, worker_actor
    from tests.coordination.test_collaborative_core import start as start_run
    from tests.coordination.test_integrator_batches import choose, ready

    hub = await new_hub(tmp_path)
    now = [1_800_000_000_000]
    hub.clock = lambda: now[0]
    try:
        await hub.db.call(hub._enable_integrator, write=True)
        run = await start_run(hub)
        await ready(hub, run, "First accepted contribution")
        now[0] += BATCH_DELAY_MS
        assignment = await choose(hub, run)
        await command(hub, "worker.bind_session", {"worker_id": assignment["worker_id"], "thread_id": "batch-one",
                      "codex_profile": hub.config.codex.model_dump(mode="json")}, RUNNER)
        await record_saved_turn(hub, assignment["worker_id"])
        batch = await hub.db.call(lambda c: hub._batch(c))
        await command(hub, "integrator.defer", {"candidate_id": batch["items"][0]["candidate_id"],
                      "reason": "Waiting for a precise source clarification."}, worker_actor(assignment))
        await command(hub, "integrator.finish_empty", {}, worker_actor(assignment))
        await quiet(hub, assignment)
        assert (await hub.worker(assignment["worker_id"]))["thread_id"] == "batch-one"
        await ready(hub, run, "Second accepted contribution")
        now[0] += BATCH_DELAY_MS
        second = await choose(hub, run)
        assert second["worker_id"] == assignment["worker_id"]
        assert (await hub.worker(assignment["worker_id"]))["thread_id"] is None
        released = await hub.db.call(lambda c: [json.loads(row[0])["reason"] for row in c.execute(
            "SELECT payload_json FROM events WHERE kind='worker.conversation_released' AND object_id=?",
            (assignment["worker_id"],))])
        assert released == ["new_batch"]
    finally:
        await hub.close()


async def test_a_question_about_a_finished_review_starts_a_new_conversation_from_its_report(tmp_path):
    from tests.coordination.publication_helpers import new_hub
    from tests.coordination.review_helpers import accept, reviewing
    from tests.coordination.test_collaborative_core import start as start_run

    hub = await new_hub(tmp_path)
    try:
        run = await start_run(hub)
        reviewer = await reviewing(hub, run, "Reviewed contribution")
        await accept(hub, reviewer)
        await hub.ordinary_turn_completed(reviewer["worker_id"], reviewer["id"])
        await command(hub, "worker.bind_session", {"worker_id": reviewer["worker_id"], "thread_id": "review-thread",
                      "codex_profile": hub.config.codex.model_dump(mode="json")}, RUNNER)
        await record_saved_turn(hub, reviewer["worker_id"])
        await command(hub, "message.send", {"worker_id": reviewer["worker_id"], "message_id": uuid4().hex,
                                            "text": "Which source page did you check?"})
        question = None
        for _ in range(10):
            assignment = await hub.next_assignment(run["id"])
            if assignment is None or assignment["worker_id"] == reviewer["worker_id"]:
                question = assignment
                break
        assert question is not None and question["execution_mode"] == "message_only"
        harness = ScriptedHarness([ScriptedTurn([])])
        await WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=question)
        assert harness.opened_threads == [None]
        prompt = json.loads(harness.prompts[0])
        report = f".private/reviews/{reviewer['task_id']}/report.md"
        assert prompt["review_report"] == report and "first read review_report" in prompt["instruction"]
        text = (hub.workspace(reviewer["worker_id"]) / report).read_text(encoding="utf-8")
        assert "- Verdict: approve" in text and "Compared the exact candidate with its task." in text
        assert "- Candidate files: `" in text  # Where a later question finds what was reviewed.
        recorded = await hub.review_report(reviewer["worker_id"])
        assert recorded["path"] == report and recorded["review_task_id"] == reviewer["task_id"]
    finally:
        await hub.close()
