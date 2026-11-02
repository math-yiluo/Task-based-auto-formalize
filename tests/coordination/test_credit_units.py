"""Each turn's own tokens, priced in credit units per mode and per finished review (TOKEN-PLAN §六)."""
from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.rotation import CREDIT_WEIGHTS, credit_units
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from tests.coordination.lifecycle_helpers import resolve_recruitment
from tests.coordination.publication_helpers import new_hub
from tests.coordination.review_helpers import accept, reviewing
from tests.coordination.test_collaborative_core import start

OPERATOR = Actor(role=Role.OPERATOR)


def sample(total_input, total_cached, total_output, last_input, last_cached, last_output):
    """A Codex usage sample: the conversation's cumulative counts and its last request's."""
    return {"totalTokens": total_input + total_output, "inputTokens": total_input,
            "cachedInputTokens": total_cached, "outputTokens": total_output,
            "lastTokenUsage": {"inputTokens": last_input, "cachedInputTokens": last_cached,
                               "outputTokens": last_output, "totalTokens": last_input + last_output}}


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    hub.clock = lambda: 1_800_000_000_000
    await hub.execute(Command(name="task.publish", key=uuid4().hex, payload={
        "contract": {"title": "Valuation bound", "objective": "Prove the bound."}}), OPERATOR)
    run = await hub.execute(Command(name="run.start", key=uuid4().hex, payload={}), OPERATOR)
    try:
        yield hub, run
    finally:
        await hub.close()


async def activate(hub, run, usage):
    await resolve_recruitment(hub, run["id"])
    assignment = await hub.next_assignment(run["id"])
    harness = ScriptedHarness([ScriptedTurn([], usage=usage)])
    await WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=assignment)
    return assignment, harness


async def turn_tokens(hub):
    return await hub.db.call(lambda c: [tuple(row) for row in c.execute(
        "SELECT u.input_tokens,u.cached_tokens,u.output_tokens,u.requests FROM turn_intents t "
        "JOIN turn_usage_totals u ON u.intent_id=t.id ORDER BY t.rowid")])


async def test_each_turn_is_priced_by_what_its_conversation_grew(project):
    hub, run = project
    first, harness = await activate(hub, run, sample(20_000, 15_000, 1_000, 20_000, 15_000, 1_000))
    # The same conversation's next turn: 30,000 more input tokens, all cached, and 1,000 output.
    _, again = await activate(hub, run, sample(50_000, 45_000, 2_000, 30_000, 30_000, 1_000))
    assert again.opened_threads == [harness.session.thread_id]
    # A turn that only repeats the last sample made no request of its own.
    await activate(hub, run, sample(50_000, 45_000, 2_000, 30_000, 30_000, 1_000))
    assert await turn_tokens(hub) == [(20_000, 15_000, 1_000, 1), (30_000, 30_000, 1_000, 1),
                                      (None, None, None, None)]
    summary = await hub.rotation_summary()
    assert summary["credit_weights"] == CREDIT_WEIGHTS
    assert summary["credits"] == [{
        "backend": "codex", "mode": "conservative", "turns": 2, "request_context": 25_000,
        "tokens_per_turn": {"uncached": 2_500, "cached": 22_500, "output": 1_000},
        # 2,500 + 0.1 × 22,500 + 5 × 1,000, and 2,500 + 0.4 × 22,500 + 34 × 1,000.
        "credits_per_turn": {"official": 9_750, "measured": 45_500}}]
    assert summary["reviews"] == []


async def test_only_the_request_just_made_counts_when_the_conversation_was_reconciled(project):
    hub, _ = project
    worker = await hub.execute(Command(name="worker.register", key=uuid4().hex, payload={}), OPERATOR)

    def charge(sample_now, previous):
        def write(c):
            intent = uuid4().hex
            c.execute("INSERT INTO turn_intents(id,worker_id,attempt_id,run_id,state,created_at) "
                      "VALUES(?,?,'a','r','bound',?)", (intent, worker["id"], hub.clock()))
            c.execute("INSERT INTO turn_usage_totals(intent_id,worker_id,epoch,start_total,updated_at) "
                      "VALUES(?,?,0,0,0)", (intent, worker["id"]))
            hub._record_turn_usage(c, worker["id"], sample_now, previous)
            row = c.execute("SELECT input_tokens,cached_tokens,output_tokens,requests FROM turn_usage_totals "
                            "WHERE intent_id=?", (intent,)).fetchone()
            c.execute("UPDATE turn_intents SET state='completed' WHERE id=?", (intent,))
            return tuple(row)
        return hub.db.call(write, write=True)

    usage = sample(900_000, 800_000, 9_000, 40_000, 30_000, 500)
    # After an operator reconciled a turn, the stored total has no counts to subtract.
    reconciled = {"totalTokens": 880_000, "source": "operator_terminal_reconciliation"}
    assert await charge(usage, reconciled) == (40_000, 30_000, 500, 1)
    # Counts that went down are no turn's usage.
    assert await charge(usage, sample(950_000, 800_000, 9_000, 1, 0, 0)) == (None, None, None, None)


async def test_finished_reviews_are_priced_by_their_reviewers_mode(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        run = await start(hub)
        conservative = await reviewing(hub, run, "Bound")
        mixed = await reviewing(hub, run, "Lift")
        unfinished = await reviewing(hub, run, "Descent")
        for reviewer in (conservative, mixed):
            await accept(hub, reviewer)
        turns = [(conservative, "conservative", (100_000, 80_000, 2_000)),
                 (mixed, "conservative", (50_000, 40_000, 1_000)), (mixed, "aggressive", (10_000, 0, 1_000)),
                 (unfinished, "conservative", (500_000, 0, 9_000))]

        def record(c):
            for reviewer, mode, (tokens, cached, output) in turns:
                intent = uuid4().hex
                c.execute("INSERT INTO turn_intents(id,worker_id,attempt_id,run_id,state,created_at,rotation_mode) "
                          "VALUES(?,?,?,?,'completed',?,?)",
                          (intent, reviewer["worker_id"], reviewer["id"], run["id"], hub.clock(), mode))
                c.execute("INSERT INTO turn_usage_totals(intent_id,worker_id,epoch,start_total,updated_at,"
                          "input_tokens,cached_tokens,output_tokens,requests) VALUES(?,?,0,0,0,?,?,?,1)",
                          (intent, reviewer["worker_id"], tokens, cached, output))
        await hub.db.call(record, write=True)
        summary = await hub.rotation_summary()
    finally:
        await hub.close()
    assert summary["reviews"] == [
        {"backend": "codex", "mode": "conservative", "reviews": 1, "median_credits": credit_units(20_000, 80_000, 2_000)},
        {"backend": "codex", "mode": "mixed", "reviews": 1, "median_credits": credit_units(20_000, 40_000, 2_000)}]
    assert credit_units(20_000, 80_000, 2_000) == {"official": 38_000, "measured": 120_000}


def test_claude_tokens_are_priced_at_list_price_ratios_only():
    # Test 2's measured weights are the Codex quota's; Claude has only the official ratios.
    assert credit_units(20_000, 80_000, 2_000, "claude") == {"official": 38_000}


async def test_claude_turns_and_reviews_are_reported_apart_from_codex(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        run = await start(hub)
        reviewer = await reviewing(hub, run, "Bound")
        await accept(hub, reviewer)
        worker = await hub.execute(Command(name="worker.register", key=uuid4().hex,
                                           payload={"backend": "claude"}), OPERATOR)

        def record(c):
            c.execute("UPDATE workers SET backend='claude' WHERE id=?", (reviewer["worker_id"],))
            for owner, tokens in ((reviewer["worker_id"], (30_000, 20_000, 1_000)), (worker["id"], (10_000, 0, 100))):
                intent = uuid4().hex
                c.execute("INSERT INTO turn_intents(id,worker_id,attempt_id,run_id,state,created_at,rotation_mode) "
                          "VALUES(?,?,'a',?,'completed',?,'aggressive')", (intent, owner, run["id"], hub.clock()))
                c.execute("INSERT INTO turn_usage_totals(intent_id,worker_id,epoch,start_total,updated_at,"
                          "input_tokens,cached_tokens,output_tokens,requests) VALUES(?,?,0,0,0,?,?,?,1)",
                          (intent, owner, *tokens))
        await hub.db.call(record, write=True)
        summary = await hub.rotation_summary()
    finally:
        await hub.close()
    # Only ordinary workers' turns are priced per turn; the review is priced as a whole.
    [turns] = [item for item in summary["credits"] if item["backend"] == "claude"]
    assert (turns["mode"], turns["turns"], turns["credits_per_turn"]) == ("aggressive", 1, {"official": 10_500})
    assert summary["reviews"] == [{"backend": "claude", "mode": "aggressive", "reviews": 1,
                                   "median_credits": {"official": 17_000}}]
