"""Frequent ledger lookups by object and by validation artifact use indexes, not table scans."""
import sqlite3

from formal_geyser.coordination.communications import COMMUNICATION_SCHEMA
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.lifecycle_schema import LIFECYCLE_SCHEMA
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.schema import LATEST_VERSION_SQL, SCHEMA


def plan(connection, query, *parameters):
    return " ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query, parameters))


def test_event_and_validation_lookups_search_an_index():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA + COMMUNICATION_SCHEMA)
    events = plan(connection, "SELECT 1 FROM events WHERE object_id=? AND kind=? LIMIT 1", "object", "kind")
    assert "event_object_idx" in events and "SCAN events" not in events
    receipts = plan(connection, "SELECT validation_state FROM native_call_receipts WHERE validation_artifact_id=?",
                    "artifact")
    assert "native_call_validation_idx" in receipts and "SCAN native_call_receipts" not in receipts


def test_a_requirement_finds_the_correction_of_an_artifact_by_an_index():
    # Waiting work follows each requirement on an artifact to its newest version on every wake-up.
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA + LIFECYCLE_SCHEMA)
    found = plan(connection, LATEST_VERSION_SQL, "artifact")
    assert "artifact_obligation_predecessor" in found and "SCAN o " not in found + " "


async def test_automatic_recovery_searches_indexes_for_admission_failures_and_attempts(tmp_path):
    # Recovery runs several times a minute: scanning every event (234,000 in the Linux pilot)
    # took 3 s a pass, and every attempt (23,000) 12.5 ms for each worker it looked at.
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        run = await hub.execute(Command(name="run.start", key="start", payload={}), Actor(role=Role.OPERATOR))
        statements: list[str] = []
        await hub.db.call(lambda c: c.set_trace_callback(statements.append))
        try:
            await hub.recover_quiet_runtime_failures(run["id"])
        finally:
            await hub.db.call(lambda c: c.set_trace_callback(None))
        [query] = [sql for sql in statements if "kind='worker.admission_failed'" in sql]
        found = await hub.db.call(lambda c: plan(c, query))
        assert "event_kind_worker_idx" in found and "SCAN e " not in found + " "
        attempts = await hub.db.call(lambda c: plan(
            c, "SELECT id,state FROM attempts WHERE worker_id=? ORDER BY created_at DESC,rowid DESC LIMIT 16", "w"))
        assert "attempts_worker_time" in attempts and "SCAN attempts" not in attempts
    finally:
        await hub.close()


async def test_requirements_on_a_task_search_that_tasks_artifacts(tmp_path):
    # Waiting work checks them on every wake-up: scanning all evidence took 5 ms a requirement
    # in the Linux pilot, for 634 of them.
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        requirement = {"key": "result", "task_id": "t", "task_revision": 1, "policy": "general.manual@1"}
        statements: list[str] = []

        def check(c):
            c.set_trace_callback(statements.append)
            try:
                return hub._requirement(c, requirement), hub._possible_requirements(c, [requirement])
            finally:
                c.set_trace_callback(None)
        assert await hub.db.call(check) == (False, set())
        queries = [sql for sql in statements if "JOIN evidence" in sql]
        assert len(queries) == 2
        for query in queries:
            found = await hub.db.call(lambda c, query=query: plan(c, query))
            assert "artifact_task_idx" in found
            assert "SCAN e " not in found + " " and "SCAN a " not in found + " "
    finally:
        await hub.close()


async def test_admission_searches_indexes_for_decisions_and_idle_workers(tmp_path):
    # Each admission pass with a free session finds every task's latest decision and every idle
    # worker's open continuations and deliveries: in the Linux pilot, reading every decision
    # (1,733) for each one and every hold (16,351) for each idle worker took 3-4 s a pass.
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        run = await hub.execute(Command(name="run.start", key="start", payload={}), Actor(role=Role.OPERATOR))
        statements: list[str] = []
        await hub.db.call(lambda c: c.set_trace_callback(statements.append))
        try:
            await hub.next_assignment(run["id"])
        finally:
            await hub.db.call(lambda c: c.set_trace_callback(None))
        [offers] = [sql for sql in statements if "'$.outcome')='new_worker_held'" in sql]
        found = await hub.db.call(lambda c: plan(c, offers))
        assert "helper_intent_task_idx" in found and "SCAN l" not in found
        [idle] = [sql for sql in statements if "FROM workers w WHERE kind='ordinary' AND work_intention='idle'" in sql]
        found = await hub.db.call(lambda c: plan(c, idle))
        assert "continuation_worker_idx" in found and "message_hold_worker_idx" in found
        assert "SCAN message_delivery_holds" not in found and "SCAN continuations" not in found
    finally:
        await hub.close()


async def test_the_drivers_validation_poll_searches_only_open_validations(tmp_path):
    # The run driver asks every couple of seconds; scanning every receipt (19,229 in the Linux
    # pilot) took the coordination thread 0.16-0.37 s a call.
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        run = await hub.execute(Command(name="run.start", key="start", payload={}), Actor(role=Role.OPERATOR))
        statements: list[str] = []
        await hub.db.call(lambda c: c.set_trace_callback(statements.append))
        try:
            assert await hub.pending_native_validations(run["id"]) == []
        finally:
            await hub.db.call(lambda c: c.set_trace_callback(None))
        [query] = [sql for sql in statements if "FROM native_call_receipts n JOIN attempts" in sql]
        found = await hub.db.call(lambda c: plan(c, query))
        assert "native_validation_open" in found and "SCAN n" not in found
    finally:
        await hub.close()
