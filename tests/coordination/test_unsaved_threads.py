"""A thread that never received a turn was never saved by Codex; start again instead of holding."""
from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from tests.coordination.lifecycle_helpers import record_saved_turn


async def command(hub, name, payload=None, *, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), Actor(role=role))


async def bind(hub, worker_id, thread_id):
    await command(hub, "worker.bind_session", {
        "worker_id": worker_id, "thread_id": thread_id, "model": "scripted", "config_hash": "retained",
        **WorkerRunner(hub, ScriptedHarness)._catalog_binding()}, role=Role.RUNNER)


async def released_threads(hub, worker_id):
    return await hub.db.call(lambda c: [row[0] for row in c.execute(
        "SELECT json_extract(payload_json,'$.thread_id') FROM events "
        "WHERE kind='worker.empty_thread_released' AND object_id=?", (worker_id,))])


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    await command(hub, "task.publish", {"contract": {
        "title": "Start the conversation", "objective": "Inspect the workspace."}})
    run = await command(hub, "run.start")
    try:
        yield hub, run
    finally:
        await hub.close()


async def test_unsaved_thread_is_released_and_the_next_activation_starts_fresh(project):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])
    # Preparation ended before the first turn: the binding names a thread Codex never saved.
    await bind(hub, attempt["worker_id"], "never-saved")
    harness = ScriptedHarness([ScriptedTurn([])])
    await WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=attempt)
    assert harness.opened_threads == [None]
    assert await released_threads(hub, attempt["worker_id"]) == ["never-saved"]
    assert (await hub.worker(attempt["worker_id"]))["thread_id"] == harness.session.thread_id


async def test_saved_thread_is_resumed(project):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])
    await bind(hub, attempt["worker_id"], "saved-thread")
    await record_saved_turn(hub, attempt["worker_id"])
    harness = ScriptedHarness([ScriptedTurn([])])
    await WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=attempt)
    assert harness.opened_threads == ["saved-thread"]
    assert await released_threads(hub, attempt["worker_id"]) == []


@pytest.mark.parametrize("prior_state,infrastructure", [("abandoned", True), ("interrupted", False)])
async def test_continuation_says_background_sessions_from_a_stopped_turn_are_gone(project, prior_state, infrastructure):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])
    await hub.db.call(lambda c: c.execute("UPDATE attempts SET state=?,ended_at=? WHERE id=?",
                                          (prior_state, hub.clock(), attempt["id"])), write=True)
    warning = await hub.prior_execution_warning(attempt["worker_id"], "next-attempt")
    assert "Terminal sessions and background commands from that turn no longer exist" in warning
    assert ("infrastructure error" in warning) is infrastructure


@pytest.mark.parametrize("earlier,warned", [("abandoned", True), ("continued", False)])
async def test_a_hold_before_any_input_is_no_error_and_hides_no_earlier_one(project, earlier, warned):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])

    def held_after(c):
        row = dict(c.execute("SELECT * FROM attempts WHERE id=?", (attempt["id"],)).fetchone())
        c.execute("UPDATE attempts SET state=?,ended_at=? WHERE id=?", (earlier, hub.clock(), attempt["id"]))
        # A later activation held (by a pause or an account hold) before it sent anything.
        row.update(id="held-attempt", state="preflight_held", created_at=row["created_at"] + 1, ended_at=hub.clock())
        c.execute(f"INSERT INTO attempts({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
    await hub.db.call(held_after, write=True)
    warning = await hub.prior_execution_warning(attempt["worker_id"], "next-attempt")
    assert (warning is not None and "infrastructure error" in warning) is warned
