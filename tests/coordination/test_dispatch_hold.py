"""An explicit Codex account error stops new dispatch until the operator resumes."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.faults import ACCOUNT_HOLDS
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    CodexConfig,
    Command,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.codex import (
    CodexHarness,
    SessionInfo,
    sign_in_identity,
)
from tests.coordination.lifecycle_helpers import assignment_decision


async def command(hub, name, payload=None, *, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), Actor(role=role))


def worker_actor(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


async def hold(hub, code="usageLimitExceeded", subject="exhausted-turn"):
    return await hub.hold_dispatch(code, "The account usage limit was reached.", subject_id=subject)


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    await command(hub, "task.publish", {"contract": {"title": "Continue the proof", "objective": "Inspect the workspace."}})
    run = await command(hub, "run.start")
    try:
        yield hub, run
    finally:
        await hub.close()


async def test_hold_stops_new_dispatch_while_running_work_continues(project):
    hub, run = project
    running = await hub.next_assignment(run["id"])
    assert await hold(hub, subject=running["id"]) is True
    # The in-flight turn keeps its authority for tool calls, checks, and merges.
    await hub.authorize_worker(worker_actor(running))
    await command(hub, "task.publish", {"contract": {"title": "Another task", "objective": "Inspect more."}})
    await command(hub, "worker.register")
    assert await hub.next_assignment(run["id"]) is None
    assert await hub.due_assignment_intents(run["id"]) == []
    assert await hub.due_recruitment_intents(run["id"]) == []
    record = await hub.run(run["id"])
    assert record["state"] == "running"
    assert record["dispatch_hold"]["message"] == ACCOUNT_HOLDS["usageLimitExceeded"]
    assert (await hub.worker(running["worker_id"]))["work_intention"] == "working"


async def test_hold_survives_restart_and_only_resume_clears_it(project):
    hub, run = project
    await hold(hub)
    assert await hold(hub, "unauthorized") is False  # The first report is kept.
    await hub.close()
    reopened = Hub(hub.root)
    try:
        with pytest.raises(HubError) as held:
            await reopened.require_dispatch()
        assert held.value.code == "DISPATCH_HELD"
        assert (await reopened.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"
        await command(reopened, "run.resume", {"run_id": run["id"]})
        assert (await reopened.run(run["id"]))["dispatch_hold"] is None
        await reopened.require_dispatch()
        assert await reopened.next_assignment(run["id"]) is not None
        # An account that is still exhausted simply holds again.
        assert await hold(reopened) is True
    finally:
        await reopened.close()


@pytest.mark.parametrize("record,held", [
    ({"code": "usageLimitExceeded", "message": "Usage limit reached"}, "usageLimitExceeded"),
    ({"code": "unauthorized", "message": "Sign in again"}, "unauthorized"),
    ({"code": "usageLimitExceeded", "message": "Retrying", "will_retry": True}, None),
    ({"code": "rateLimitExceeded", "message": "Slow down"}, None),
    ({"code": "httpConnectionFailed", "message": "Network"}, None),
])
async def test_only_final_account_errors_hold_dispatch(project, record, held):
    hub, run = project
    await WorkerRunner(hub, ScriptedHarness)._note_account_hold(record, subject_id="turn", worker_id=None)
    hold_record = (await hub.run(run["id"]))["dispatch_hold"]
    assert (hold_record["code"] if hold_record else None) == held


async def test_claimed_activation_waits_without_launching_a_process(project):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])
    await hold(hub)
    harness = ScriptedHarness([ScriptedTurn([])])
    await WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=attempt)
    assert harness.opened_threads == [] and harness.prompts == []
    assert (await hub.worker(attempt["worker_id"]))["active_attempt"] is None


async def test_hold_during_preparation_records_the_reserved_turn_as_not_sent(project):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])

    class HeldWhilePreparing(ScriptedHarness):
        async def open(self, **kwargs):
            session = await super().open(**kwargs)
            await hold(hub)  # Another worker reported the exhausted account meanwhile.
            return session

    harness = HeldWhilePreparing([ScriptedTurn([])])
    with pytest.raises(HubError) as held:
        await WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=attempt)
    assert held.value.code == "DISPATCH_HELD"
    assert harness.prompts == []
    states = await hub.db.call(lambda c: [row[0] for row in c.execute(
        "SELECT state FROM turn_intents WHERE worker_id=?", (attempt["worker_id"],))])
    assert states == ["not_sent"]


async def test_prepared_helper_waits_instead_of_being_sent(project):
    hub, run = project
    await command(hub, "worker.register")  # An idle worker: the open task waits for its decision.
    intent = await assignment_decision(hub, run["id"])
    await hold(hub)
    harness = ScriptedHarness([ScriptedTurn([])])
    with pytest.raises(HubError) as held:
        await WorkerRunner(hub, lambda: harness).run_helper(intent)
    assert held.value.code == "DISPATCH_HELD"
    assert harness.prompts == []


async def test_hold_reported_while_a_helper_prepares_stops_its_turn(project, monkeypatch):
    hub, run = project
    await command(hub, "worker.register")
    intent = await assignment_decision(hub, run["id"])
    original, calls = hub.run, []

    async def reported_meanwhile(run_id, **options):
        calls.append(run_id)
        if len(calls) == 1:  # The run-control check: the last preparation step before the dispatch check.
            await hold(hub)  # Another execution reports the exhausted account meanwhile.
        return await original(run_id, **options)
    monkeypatch.setattr(hub, "run", reported_meanwhile)
    harness = ScriptedHarness([ScriptedTurn([])])
    with pytest.raises(HubError) as held:
        await WorkerRunner(hub, lambda: harness).run_helper(intent)
    assert held.value.code == "DISPATCH_HELD"
    assert calls and harness.opened_threads and harness.prompts == []


async def test_account_errors_reach_the_hold_after_the_diagnostic_log_is_full():
    harness = CodexHarness(CodexConfig(), command=["codex"])
    logged, controlled = [], []

    async def log(record):
        logged.append(record)

    async def control(record):
        controlled.append(record.get("code"))
    harness.set_diagnostic_handler(log)
    harness.set_control_handler(control)
    for _ in range(40):
        await harness._record_error({"message": "Transient", "codexErrorInfo": "internalServerError"},
                                    source="notification", will_retry=True)
    await harness._record_error({"message": "Usage limit reached", "codexErrorInfo": "usageLimitExceeded"},
                                source="notification", will_retry=False)
    # Logging stays bounded; the control signal is never dropped.
    assert len(logged) == 32
    assert controlled[-1] == "usageLimitExceeded" and len(controlled) == 41


async def test_runner_routes_harness_account_errors_to_the_hold(project):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])

    class Exhausted(ScriptedHarness):
        def set_control_handler(self, handler):
            self.control = handler

        async def open(self, **kwargs):
            session = await super().open(**kwargs)
            await self.control({"code": "usageLimitExceeded", "message": "Usage limit reached"})
            return session

    with pytest.raises(HubError):
        await WorkerRunner(hub, lambda: Exhausted([ScriptedTurn([])])).run_once(run["id"], assignment=attempt)
    assert (await hub.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"


def test_a_codex_process_sees_a_sign_in_made_after_it_started(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text('{"tokens": "first"}', encoding="utf-8")
    harness = CodexHarness(CodexConfig(home=str(home)), command=["codex"])
    assert not harness.signed_in_again()  # No process has started.
    harness._sign_in, harness.host = sign_in_identity(home), object()  # As open() records them.
    assert not harness.signed_in_again()
    replacement = home / "auth.json.new"
    replacement.write_text('{"tokens": "second"}', encoding="utf-8")
    os.replace(replacement, home / "auth.json")  # A new sign-in replaces the file.
    assert harness.signed_in_again()


@pytest.mark.parametrize("code,again,held", [
    ("unauthorized", True, None),
    ("unauthorized", False, "unauthorized"),
    ("usageLimitExceeded", True, "usageLimitExceeded"),
])
async def test_a_sign_in_error_of_a_process_older_than_the_sign_in_holds_nothing(project, code, again, held):
    # Each sign-in revokes the tokens the running processes keep: in the pilot nine holds in 40
    # minutes came from processes started before the latest sign-in, and new ones worked.
    hub, run = project

    class Process:
        def signed_in_again(self):
            return again
    await WorkerRunner(hub, ScriptedHarness)._note_account_hold(
        {"code": code, "message": "Your access token could not be refreshed"},
        subject_id="turn", worker_id=None, harness=Process())
    hold_record = (await hub.run(run["id"]))["dispatch_hold"]
    assert (hold_record["code"] if hold_record else None) == held


async def test_an_activation_reports_its_own_process_with_a_sign_in_error(project):
    hub, run = project
    attempt = await hub.next_assignment(run["id"])

    class Stale(ScriptedHarness):
        def set_control_handler(self, handler):
            self.control = handler

        def signed_in_again(self):
            return True

        async def open(self, **kwargs):
            session = await super().open(**kwargs)
            await self.control({"code": "unauthorized", "message": "Sign in again"})
            return session

    await WorkerRunner(hub, lambda: Stale([ScriptedTurn([])])).run_once(run["id"], assignment=attempt)
    assert (await hub.run(run["id"]))["dispatch_hold"] is None


async def test_hold_stops_dispatch_in_memory_when_its_write_fails_and_is_persisted_later(project, monkeypatch):
    hub, run = project
    original, failures = hub.db.call, [sqlite3.OperationalError("database is locked")]

    async def flaky(function, write=False, **options):
        if write and failures:
            raise failures.pop()
        return await original(function, write=write, **options)
    monkeypatch.setattr(hub.db, "call", flaky)

    async def stored():
        return await hub.db.call(lambda c: c.execute("SELECT count(*) FROM dispatch_hold").fetchone()[0])
    await WorkerRunner(hub, ScriptedHarness)._note_account_hold(
        {"code": "usageLimitExceeded", "message": "Usage limit reached"}, subject_id="turn", worker_id=None)
    assert await stored() == 0
    # The control decision is kept: dispatch is already stopped in this process.
    assert await hub.next_assignment(run["id"]) is None
    assert (await hub.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"
    with pytest.raises(HubError):
        await hub.require_dispatch()
    # The admission loop writes it once the database accepts writes again.
    await WorkerRunner(hub, ScriptedHarness).run_until_idle(run["id"])
    assert await stored() == 1
    await command(hub, "run.resume", {"run_id": run["id"]})
    assert (await hub.run(run["id"]))["dispatch_hold"] is None
    assert await hub.next_assignment(run["id"]) is not None


async def test_failed_transcript_capture_never_hides_an_account_error():
    harness = CodexHarness(CodexConfig(), command=["codex"])
    harness.session = SessionInfo("thread-1", "model", "config")
    harness._turn_id = "turn-1"
    harness._turn_terminal = asyncio.get_running_loop().create_future()
    controlled = []

    async def capture(method, params):
        raise PermissionError("The transcript file is locked")

    async def control(record):
        controlled.append(record.get("code"))
    harness.set_native_handler(capture)
    harness.set_control_handler(control)
    await harness._notification("turn/completed", {"threadId": "thread-1", "turn": {
        "id": "turn-1", "status": "failed",
        "error": {"message": "Usage limit reached", "codexErrorInfo": "usageLimitExceeded"}}})
    assert controlled == ["usageLimitExceeded"]
    assert harness._turn_terminal.result() == "failed"


async def test_hold_whose_database_write_failed_survives_a_restart(project, monkeypatch):
    hub, run = project
    original = hub.db.call

    async def unwritable(function, write=False, **options):
        if write:
            raise sqlite3.OperationalError("database is locked")
        return await original(function, write=write, **options)
    monkeypatch.setattr(hub.db, "call", unwritable)
    await WorkerRunner(hub, ScriptedHarness)._note_account_hold(
        {"code": "usageLimitExceeded", "message": "Usage limit reached"}, subject_id="turn", worker_id=None)
    monkeypatch.undo()
    await hub.close()  # The service stops before any retry wrote the row.
    reopened = Hub(hub.root)
    try:
        assert (await reopened.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"
        assert await reopened.next_assignment(run["id"]) is None
        await command(reopened, "run.resume", {"run_id": run["id"]})
        assert not (reopened.state_dir / "dispatch-hold.json").exists()
    finally:
        await reopened.close()
    again = Hub(hub.root)
    try:
        # Resume released it for good: a quota reset or restart alone never does.
        assert (await again.run(run["id"]))["dispatch_hold"] is None
    finally:
        await again.close()


async def test_unreadable_hold_marker_still_holds_dispatch(project):
    hub, run = project
    await hub.close()
    (hub.state_dir / "dispatch-hold.json").write_text("{not json", encoding="utf-8")
    reopened = Hub(hub.root)
    try:
        held = (await reopened.run(run["id"]))["dispatch_hold"]
        assert held["code"] == "unknown" and "could not be read" in held["message"]
        with pytest.raises(HubError):
            await reopened.require_dispatch()
    finally:
        await reopened.close()


async def exhausted(hub):
    await WorkerRunner(hub, ScriptedHarness)._note_account_hold(
        {"code": "usageLimitExceeded", "message": "Usage limit reached"}, subject_id="turn", worker_id=None)


async def test_a_write_queued_before_resume_cannot_revive_the_released_hold(project, monkeypatch):
    hub, run = project
    original, queued, failed = hub.db.call, asyncio.Event(), []

    async def gated(function, write=False, **options):
        if write and function.__name__ == "persist":
            if not failed:
                failed.append(1)
                raise sqlite3.OperationalError("database is locked")  # Only memory and marker hold it.
            await queued.wait()  # The retry has its hold in hand; Resume commits first.
        return await original(function, write=write, **options)
    monkeypatch.setattr(hub.db, "call", gated)
    await exhausted(hub)
    retry = asyncio.create_task(hub.persist_pending_dispatch_hold())
    await asyncio.sleep(0)
    await command(hub, "run.resume", {"run_id": run["id"]})
    queued.set()
    assert await retry is False
    assert (await hub.run(run["id"]))["dispatch_hold"] is None
    assert await original(lambda c: c.execute("SELECT count(*) FROM dispatch_hold").fetchone()[0]) == 0


async def test_failed_resume_leaves_a_memory_and_marker_hold_in_place(project, monkeypatch):
    hub, run = project
    original = hub.db.call

    async def failing(function, write=False, **options):
        if write and function.__name__ == "persist":
            raise sqlite3.OperationalError("database is locked")
        if write and function.__name__ == "apply":
            def apply_then_fail(c):
                function(c)  # Resume's own steps run; the transaction then fails and rolls back.
                raise sqlite3.OperationalError("disk I/O error")
            return await original(apply_then_fail, write=True, **options)
        return await original(function, write=write, **options)
    monkeypatch.setattr(hub.db, "call", failing)
    await exhausted(hub)
    with pytest.raises(sqlite3.OperationalError):
        await command(hub, "run.resume", {"run_id": run["id"]})
    assert (await hub.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"
    assert (hub.state_dir / "dispatch-hold.json").exists()
    monkeypatch.undo()
    await hub.close()
    reopened = Hub(hub.root)
    try:
        assert (await reopened.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"
    finally:
        await reopened.close()


@pytest.mark.parametrize("marker", ['{"code": []}', '[]', '"held"',
                                    '{"code": {"x": 1}, "hold_id": 7, "created_at": "soon", "subject_id": []}'])
async def test_malformed_marker_fields_still_hold_and_resume_releases_them(project, marker):
    hub, run = project
    await hub.close()
    (hub.state_dir / "dispatch-hold.json").write_text(marker, encoding="utf-8")
    reopened = Hub(hub.root)
    try:
        assert (await reopened.run(run["id"]))["dispatch_hold"]["code"] == "unknown"
        await command(reopened, "run.resume", {"run_id": run["id"]})
        assert (await reopened.run(run["id"]))["dispatch_hold"] is None
    finally:
        await reopened.close()
    again = Hub(hub.root)
    try:
        assert (await again.run(run["id"]))["dispatch_hold"] is None
    finally:
        await again.close()


async def test_a_marker_left_by_a_committed_resume_is_not_restored(project, monkeypatch):
    hub, run = project
    await exhausted(hub)
    monkeypatch.setattr(hub, "finish_dispatch_release", _skip)  # Removing the file is interrupted.
    await command(hub, "run.resume", {"run_id": run["id"]})
    assert (hub.state_dir / "dispatch-hold.json").exists()
    await hub.close()
    reopened = Hub(hub.root)
    try:
        assert (await reopened.run(run["id"]))["dispatch_hold"] is None
        assert not (reopened.state_dir / "dispatch-hold.json").exists()
    finally:
        await reopened.close()


async def _skip():
    return None


async def released_ids(hub):
    return await hub.db.call(lambda c: [row[0] for row in c.execute("SELECT hold_id FROM released_dispatch_holds")])


async def test_a_new_report_during_resume_cleanup_is_a_new_hold(project, monkeypatch):
    hub, run = project
    original, failures = hub.db.call, [sqlite3.OperationalError("database is locked")]

    async def first_write_fails(function, write=False, **options):
        if write and function.__name__ == "persist" and failures:
            raise failures.pop()
        return await original(function, write=write, **options)
    monkeypatch.setattr(hub.db, "call", first_write_fails)
    await exhausted(hub)  # The old hold exists in memory and its marker only.
    cleanup, finish = asyncio.Event(), hub.finish_dispatch_release

    async def delayed_cleanup():
        await cleanup.wait()
        await finish()
    monkeypatch.setattr(hub, "finish_dispatch_release", delayed_cleanup)
    resume = asyncio.create_task(command(hub, "run.resume", {"run_id": run["id"]}))
    async with asyncio.timeout(10):
        while not await released_ids(hub):
            await asyncio.sleep(0.01)
    # Resume committed; its cleanup has not run. Another worker now reports the limit.
    await exhausted(hub)
    cleanup.set()
    await resume
    assert (await hub.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"
    with pytest.raises(HubError):
        await hub.require_dispatch()


async def test_resume_cleanup_never_removes_a_newer_marker(project, monkeypatch):
    hub, run = project
    original, failing, paused, proceed = hub.db.call, [True], asyncio.Event(), asyncio.Event()

    async def gated(function, write=False, **options):
        if write and function.__name__ == "persist" and failing[0]:
            raise sqlite3.OperationalError("database is locked")
        if "finish_dispatch_release" in getattr(function, "__qualname__", "") and not paused.is_set():
            paused.set()
            await proceed.wait()  # Cleanup has read the old marker and waits on the database.
        return await original(function, write=write, **options)
    monkeypatch.setattr(hub.db, "call", gated)
    await exhausted(hub)
    resume = asyncio.create_task(command(hub, "run.resume", {"run_id": run["id"]}))
    async with asyncio.timeout(10):
        await paused.wait()
    await exhausted(hub)  # A new hold: its marker is written, its database write fails.
    newer = hub._pending_dispatch_holds["codex"]["hold_id"]
    proceed.set()
    await resume
    marker = json.loads((hub.state_dir / "dispatch-hold.json").read_text(encoding="utf-8"))
    assert marker["hold_id"] == newer
    failing[0] = False
    monkeypatch.undo()
    await hub.close()  # A restart before the new hold's database write succeeded.
    reopened = Hub(hub.root)
    try:
        assert (await reopened.run(run["id"]))["dispatch_hold"]["code"] == "usageLimitExceeded"
    finally:
        await reopened.close()
