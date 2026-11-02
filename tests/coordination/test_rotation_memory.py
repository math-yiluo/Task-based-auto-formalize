"""Whether a rotated conversation kept the old one's working memory (TOKEN-PLAN §9.7)."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.rotation_review import (
    BASELINES,
    classify,
    first_work,
    memory_report,
    touched,
)
from formal_geyser.coordination.transcripts import TranscriptStore
from formal_geyser.lean_service.store import Store

SWITCH_MS = 1_700_000_000_000  # When the new conversation started (in the past).


def call(tool, thread, *, operation=None, arguments=None, command=None, paths=None, error=None):
    event = {"type": "action", "action_id": uuid4().hex, "thread_id": thread, "tool": tool,
             "status": "error" if error else "running", "at": "2023-11-14T22:13:20+00:00"}
    if operation:
        event["operation"] = operation
        event["arguments"] = {"operation": operation, "arguments": arguments or {}}
    if command:
        event["details"] = {"command": command}
    if paths:
        event["details"] = {"paths": paths}
    if error:
        event["result"] = {"code": error, "error": "Synthetic failure"}
    return event


def test_orienting_calls_are_schemas_guides_listings_git_and_probes():
    thread = "t"
    assert classify(call("taskhub_actions", thread)) == "orientation"
    assert classify(call("commandExecution", thread, command="cat .private/instructions/worker.md")) == "orientation"
    assert classify(call("commandExecution", thread, command="git status")) == "orientation"
    assert classify(call("commandExecution", thread, command="ls -R")) == "orientation"
    assert classify(call("taskhub_call", thread, operation="workspace_list")) == "orientation"
    assert classify(call("taskhub_call", thread, operation="workspace_read",
                         arguments={"path": ".private/taskhub-reference/operations/README.md"})) == "orientation"
    # Work: editing, checking, submitting, and reading the task's own material.
    assert classify(call("fileChange", thread, paths=["Valuation/Bound.lean"])) == "work"
    assert classify(call("taskhub_call", thread, operation="lean_check")) == "work"
    assert classify(call("taskhub_call", thread, operation="workspace_read",
                         arguments={"path": "Valuation/Bound.lean"})) == "work"
    assert classify(call("commandExecution", thread, command="/bin/bash -lc 'sed -n 1,80p Valuation/Bound.lean'")) == "work"
    # TaskHub reads and messages are neither.
    assert classify(call("taskhub_call", thread, operation="sync")) == "other"
    assert classify(call("commandExecution", thread, command="echo done")) == "other"
    assert touched(call("commandExecution", thread, command="/bin/bash -lc 'cat A/B.lean'")) == ["A/B.lean"]
    calls = [call("taskhub_actions", thread), call("taskhub_call", thread, operation="sync"),
             call("fileChange", thread, paths=["A.lean"])]
    assert first_work(calls) == (2, 1) and first_work(calls[:2]) == (None, None)


@pytest.fixture
async def hub(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        yield hub
    finally:
        await hub.close()


def write_transcript(hub, worker_id, records):
    path = TranscriptStore(hub.root).events_path(worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def add_check(store, owner, submitted_at, ok, error=None):
    result = {"compilation": {"ok": ok, "diagnostics": {"errors": int(bool(error)), "messages": (
        [{"severity": "error", "pos": {"line": 3, "column": 0}, "data": error}] if error else [])}}}
    store.insert({"id": uuid4().hex, "estimate_key": "e", "lane": "interactive", "kind": "check", "owner": owner,
                  "label": "lean_check", "spec_json": "{}", "state": "done", "outcome": "completed",
                  "result_json": json.dumps(result), "submitted_at": submitted_at})


async def test_the_report_compares_the_conversations_before_and_after_a_rotation(hub):
    worker = await hub.execute(Command(name="worker.register", key=uuid4().hex, payload={}),
                               Actor(role=Role.OPERATOR))
    worker_id = worker["id"]

    def rotation(c):
        c.execute("INSERT INTO rotations(id,worker_id,task_id,thread_id,reason,mode,state,new_thread_id,"
                  "requested_at,completed_at) VALUES('r1',?,NULL,'old','context','aggressive','completed','new',?,?)",
                  (worker_id, SWITCH_MS - 60_000, SWITCH_MS))
    await hub.db.call(rotation, write=True)
    write_transcript(hub, worker_id, [
        call("fileChange", "old", paths=["Valuation/Bound.lean"]),
        call("taskhub_call", "old", operation="workspace_read", arguments={"path": "notes/plan.md"}),
        call("taskhub_call", "old", operation="stage", error="STALE_REVISION"),
        call("taskhub_actions", "new"),
        call("commandExecution", "new", command="cat .private/instructions/worker.md"),
        call("taskhub_call", "new", operation="sync"),
        call("taskhub_call", "new", operation="workspace_read", arguments={"path": "Valuation/Bound.lean"}),
        call("fileChange", "new", paths=["Valuation/Other.lean"]),
        call("taskhub_call", "new", operation="stage", error="STALE_REVISION"),
    ])
    store = Store(hub.state_dir / "lean-service" / "service.sqlite3")
    switch = SWITCH_MS / 1000
    add_check(store, worker_id, switch - 100, False, "unknown identifier 'x'")
    add_check(store, worker_id, switch + 100, False, "unknown  identifier 'x'")
    add_check(store, worker_id, switch + 200, True)
    add_check(store, "someone-else", switch + 300, False, "unknown identifier 'x'")

    report = await hub.rotation_memory_report()
    [record] = report["rotations"]
    assert (record["mode"], record["worker_kind"], record["reason"]) == ("aggressive", "ordinary", "context")
    # Three calls, two of them orienting, came before the first work: reading the module.
    assert (record["actions_before_first_work"], record["orientation_before_first_work"]) == (3, 2)
    assert record["reopened"] == {"files": 3, "touched_before": 1, "share": 0.333}
    assert record["repeated_error_codes"] == ["STALE_REVISION"]
    assert record["checks"] == {"before": {"count": 1, "failed": 1}, "after": {"count": 2, "failed": 1}}
    assert record["repeated_compile_errors"] == ["unknown identifier 'x'"]
    [summary] = report["summary"]
    assert summary["actions_before_first_work"] == {"median": 3, "baseline": 2}
    assert summary["orientation_before_first_work"] == {"median": 2, "baseline": 2}
    assert summary["reopened_share"] == {"mean": 0.333, "baseline": 0.265}
    assert summary["check_failure_share"] == {"before": 1.0, "after": 0.5}
    assert summary["with_repeated_error_codes"] == summary["with_repeated_compile_errors"] == 1
    assert report["baselines"] == BASELINES


def test_reviews_before_and_after_are_bounded_by_the_workers_own_switches(tmp_path):
    first, second = SWITCH_MS, SWITCH_MS + 3_600_000
    rotations = [{"id": name, "worker_id": "w", "thread_id": old, "new_thread_id": new, "mode": "conservative",
                  "reason": "idle", "completed_at": at, "worker_kind": "review"}
                 for name, old, new, at in (("r1", "a", "b", first), ("r2", "b", "c", second))]
    reviews = [{"producer": "w", "submitted_at": first - 1000, "verdict": "request_changes"},
               {"producer": "w", "submitted_at": first + 1000, "verdict": "approve"},
               {"producer": "w", "submitted_at": second + 1000, "verdict": "uncertain"},
               {"producer": "other", "submitted_at": first + 1000, "verdict": "request_changes"}]
    report = memory_report(rotations, reviews, lambda worker: tmp_path / "absent.jsonl", None,
                           now=(second + 7_200_000) / 1000)
    one, two = report["rotations"]
    assert one["reviews"] == {"before": {"verdicts": 1, "returned": 1}, "after": {"verdicts": 1, "returned": 0}}
    assert two["reviews"] == {"before": {"verdicts": 1, "returned": 0}, "after": {"verdicts": 1, "returned": 1}}
    assert one["actions_before_first_work"] is None and one["reopened"]["share"] is None
    [summary] = report["summary"]
    assert summary["review_returned_share"] == {"before": 0.5, "after": 0.5}
    assert summary["orientation_before_first_work"] == {"median": None, "baseline": 2}
    assert summary["reopened_share"] == {"mean": None, "baseline": 0.334}


def test_operators_read_the_rotation_reports_through_the_api(tmp_path):
    from fastapi.testclient import TestClient

    from formal_geyser.coordination.api import create_app

    initialize(tmp_path, ProjectConfig())
    with TestClient(create_app(tmp_path)) as client:
        summary = client.get("/v1/runtime/rotation")
        memory = client.get("/v1/runtime/rotation/memory")
    assert summary.status_code == memory.status_code == 200, memory.text
    assert memory.json() == {"window": 25, "baselines": BASELINES, "summary": [], "rotations": []}
