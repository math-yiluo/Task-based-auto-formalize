import asyncio
import copy
import json
import os
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

from formal_geyser.coordination.config import initialize, load_config
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Limits, ProjectConfig, TaskContract
from formal_geyser.coordination.monitoring import (
    MonitorPolicy,
    RuntimeTail,
    SnapshotReader,
    Watchdog,
    _driver_summary,
    main,
    process_identity,
    read_database,
)


def observation(now=600_000):
    return {
        "observed_at_ms": now,
        "database": {
            "available": True,
            "run": {
                "id": "run", "state": "running", "tokens_used": 20, "turns_used": 1,
                "usage_uncertain": 0,
                "limits": Limits().model_dump(),
            },
            "tasks": [],
            "workers": [{
                "id": "worker", "state": "running", "active_attempt": "attempt",
                "created_at": 1000, "last_activity_at": 1000, "progress_key": [1, 1, 1],
            }],
            "attempts": [{
                "id": "attempt", "worker_id": "worker", "state": "running",
            }],
            "turns": [{
                "id": "turn", "worker_id": "worker", "state": "bound", "created_at": 1000,
            }],
            "continuations": [],
            "events": [],
        },
        "service": {
            "observed": True, "reachable": True,
            "run": {"id": "run", "driving": True, "armed": True, "driver_result": None},
        },
        "worker_processes": {},
        "telemetry_gaps": [],
    }


def opened(records):
    return {row["code"]: row for row in records
            if row["type"] == "incident" and row["event"] in {"opened", "updated"}}


async def create_database(root):
    """The database TaskHub creates: opened once, then closed."""
    hub = Hub(root)
    try:
        await hub.db.call(lambda c: None)
    finally:
        await hub.close()


@pytest.fixture
def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    asyncio.run(create_database(tmp_path))
    path = tmp_path / ".task-based" / "state.sqlite3"
    contract = TaskContract(title="Software record", objective="Review a software record")
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO runs(id,state,limits_json,created_at) VALUES(?,?,?,?)", (
            "run", "running", Limits().model_dump_json(), 1000,
        ))
        connection.execute(
            "INSERT INTO tasks(id,current_revision,state,active_attempt,created_at) VALUES(?,1,?,?,?)",
            ("task", "claimed", "attempt", 1000),
        )
        connection.execute("INSERT INTO task_revisions VALUES(?,?,?,?)", (
            "task", 1, contract.model_dump_json(), "contract-hash",
        ))
        connection.execute(
            "INSERT INTO workers(id,state,active_attempt,run_id,created_at) VALUES(?,?,?,?,?)",
            ("worker", "running", "attempt", "run", 1000),
        )
        connection.execute(
            "INSERT INTO attempts(id,task_id,task_revision,worker_id,run_id,fencing_token,"
            "state,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("attempt", "task", 1, "worker", "run", 1, "running", 1000),
        )
        connection.execute(
            "INSERT INTO turn_intents(id,worker_id,attempt_id,run_id,remote_turn_id,state,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            ("turn", "worker", "attempt", "run", "remote-turn", "bound", 1000),
        )
        connection.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (
            "worker:worker", "task.checkpoint", "checkpoint", "hash", "{}",
        ))
        connection.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (
            "worker:worker", "usage.record", "runtime:usage", "hash", "{}",
        ))
    return tmp_path


def test_read_only_snapshot_never_triggers_hub_recovery(project):
    result = read_database(project, "run")
    assert result["available"]
    assert result["run"]["state"] == "running"
    assert result["workers"][0]["state"] == "running"
    assert result["turns"][0]["state"] == "bound"
    assert result["workers"][0]["recorded_tool_commands"] == 1
    assert result["workers"][0]["recent_commands"][0]["command"] == "task.checkpoint"
    with sqlite3.connect(project / ".task-based" / "state.sqlite3") as connection:
        assert connection.execute("SELECT state FROM workers").fetchone()[0] == "running"
        assert connection.execute("SELECT state FROM turn_intents").fetchone()[0] == "bound"
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_same_task_peer_events_do_not_mask_worker_stall(project):
    before = read_database(project, "run")["workers"][0]
    path = project / ".task-based" / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO events(kind,object_id,task_id,worker_id,payload_json,created_at)"
            " VALUES('task.claimed','task','task','other-worker','{}',500000)"
        )
        connection.execute(
            "INSERT INTO events(kind,object_id,task_id,payload_json,created_at)"
            " VALUES('artifact.accepted','artifact','task','{}',500001)"
        )
    after = read_database(project, "run")["workers"][0]
    assert after["last_activity_at"] == before["last_activity_at"]
    assert after["progress_key"] == before["progress_key"]


def test_collaborative_task_monitor_tracks_individual_attempts_and_runnable_peers(project):
    with sqlite3.connect(project / ".task-based/state.sqlite3") as connection:
        connection.execute(
            "INSERT INTO task_collaboration(task_id,mode,governance,created_by,created_at)"
            " VALUES('task','collaborative','operator','operator',1000)"
        )
        connection.execute(
            "INSERT INTO workers(id,state,run_id,created_at) VALUES('peer','idle','run',1000)"
        )
        connection.execute(
            "INSERT INTO task_members(task_id,worker_id,executor,runnable,joined_at)"
            " VALUES('task','worker',1,0,1000),('task','peer',1,1,1001)"
        )
    result = read_database(project, "run")
    task = result["tasks"][0]
    assert task["active_attempts"] == ["attempt"]
    assert task["actionable"]
    assert task["collaboration"]["runnable_members"] == 1
    with sqlite3.connect(project / ".task-based/state.sqlite3") as connection:
        connection.execute("UPDATE task_members SET runnable=0 WHERE worker_id='peer'")
        connection.execute("UPDATE workers SET state='running',active_attempt='peer-attempt' WHERE id='peer'")
        connection.execute(
            "INSERT INTO attempts(id,task_id,task_revision,worker_id,run_id,fencing_token,state,"
            "created_at) VALUES('peer-attempt','task',1,'peer','run',2,'running',1001)"
        )
    updated = read_database(project, "run")["tasks"][0]
    assert updated["active_attempts"] == ["attempt", "peer-attempt"]
    assert not updated["actionable"]


def test_stall_is_reported_once_and_turn_has_no_deadline():
    watchdog = Watchdog()
    current = observation()
    first = opened(watchdog.observe(current))
    assert "turn_progress_stalled" in first
    assert "turn_overdue" not in first
    later = copy.deepcopy(current)
    later["observed_at_ms"] += 20_000
    assert opened(watchdog.observe(later)) == {}


def test_monitor_uses_open_collaborator_opportunity_and_excludes_departed_members(project):
    from formal_geyser.coordination.lifecycle_schema import LIFECYCLE_SCHEMA

    with sqlite3.connect(project / ".task-based/state.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(LIFECYCLE_SCHEMA)
        connection.execute("INSERT INTO task_collaboration(task_id,mode,governance,created_by,created_at) "
                           "VALUES('task','collaborative','publishers','worker',1000)")
        connection.execute("INSERT INTO task_members(task_id,worker_id,executor,runnable,joined_at) "
                           "VALUES('task','worker',1,0,1000)")
        connection.execute("INSERT INTO workers(id,state,created_at) VALUES('former','idle',1000)")
        connection.execute("INSERT INTO task_members(task_id,worker_id,executor,runnable,joined_at,membership_state) "
                           "VALUES('task','former',1,1,1001,'left')")
        connection.execute("INSERT INTO task_lifecycle(task_id,initial_worker_id,recruitment_decision,"
                           "opportunity_state,opportunity_opened_at) VALUES('task','worker','yes','open',2000)")
    task = read_database(project, "run")["tasks"][0]
    assert task["actionable"]
    assert task["collaboration"]["member_count"] == 1
    assert task["collaboration"]["runnable_members"] == 0
    assert task["collaboration"]["collaborator_opportunity"]["opportunity_state"] == "open"
    with sqlite3.connect(project / ".task-based/state.sqlite3") as connection:
        connection.execute("UPDATE task_lifecycle SET opportunity_state='cancelled' WHERE task_id='task'")
    assert not read_database(project, "run")["tasks"][0]["actionable"]


def test_new_tool_progress_resolves_stall_without_a_turn_deadline():
    watchdog = Watchdog()
    first = observation()
    watchdog.observe(first)
    second = copy.deepcopy(first)
    second["observed_at_ms"] += 1000
    second["database"]["workers"][0]["progress_key"] = [1, 1, 2]
    records = watchdog.observe(second)
    resolved = {row["code"] for row in records
                if row["type"] == "incident" and row["event"] == "resolved"}
    assert "turn_progress_stalled" in resolved
    assert "turn_overdue" not in resolved


def test_unknown_usage_never_requests_blind_retry():
    current = observation()
    current["database"]["run"]["usage_uncertain"] = 1
    current["database"]["turns"][0]["state"] = "uncertain"
    incidents = opened(Watchdog().observe(current))
    assert {"usage_uncertain", "turn_outcome_unknown"} <= incidents.keys()
    assert "do not retry" in incidents["turn_outcome_unknown"]["action"]
    assert "turn_overdue" not in incidents


def test_pause_and_manual_review_are_expected_holds_not_process_failures():
    current = observation()
    current["database"]["run"].update(state="paused", tokens_used=1000)
    current["database"]["turns"] = []
    current["database"]["workers"] = []
    current["database"]["attempts"] = []
    current["database"]["tasks"] = [{
        "id": "task", "state": "verifying", "actionable": False,
        "acceptance_policy": "general.manual@1",
        "latest_validation": {"id": "candidate", "created_at": 1, "verdict": "needs_operator"},
    }]
    incidents = opened(Watchdog().observe(current))
    assert incidents["run_paused"]["category"] == "expected_hold"
    manual = incidents["operator_review_required"]
    assert manual["category"] == "expected_hold" and manual["severity"] == "warning"
    assert "monitor never accepts" in manual["action"]


def test_idle_actionable_work_gets_a_grace_period_and_reports_unarmed_status():
    watchdog = Watchdog(MonitorPolicy(idle_grace_seconds=3))
    current = observation(10_000)
    current["database"]["turns"] = []
    current["database"]["tasks"] = [{"id": "task", "state": "ready", "actionable": True}]
    current["service"]["run"].update(driving=False, armed=False)
    assert "execution_unarmed" not in opened(watchdog.observe(current))
    current["observed_at_ms"] = 13_000
    assert "execution_unarmed" in opened(watchdog.observe(current))
    current["observed_at_ms"] = 14_000
    current["service"]["run"]["driving"] = True
    assert any(row.get("event") == "resolved" and row.get("code") == "execution_unarmed"
               for row in watchdog.observe(current))


def test_pending_validation_and_attention_states_are_actionable():
    current = observation()
    current["database"]["tasks"] = [
        {"id": "review", "state": "verifying", "acceptance_policy": "general.json@1",
         "latest_validation": {"id": "candidate", "created_at": 1000, "verdict": None}},
        {"id": "attention", "state": "needs_attention"},
    ]
    incidents = opened(Watchdog().observe(current))
    assert incidents["validation_pending"]["severity"] == "warning"
    assert incidents["validation_pending"]["details"]["observation_threshold_exceeded"]
    assert "task_needs_attention" in incidents


def test_http_probe_uses_only_get_without_credentials(project):
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        assert "Authorization" not in request.headers
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={"armed_runs": ["run"], "running_drivers": ["run"]})
        return httpx.Response(200, json={"id": "run", "state": "running",
                                        "driving": True, "armed": True})

    snapshot = SnapshotReader(
        project, url="http://127.0.0.1:8769", run_id="run",
        transport=httpx.MockTransport(respond),
    ).collect(now_ms=5000)
    assert snapshot["service"]["reachable"]
    assert calls == [("GET", "/v1/status"), ("GET", "/v1/runs/run")]


def test_service_failure_and_reused_pid_are_distinct_incidents(project):
    def fail(request):
        raise httpx.ConnectError("Unavailable", request=request)

    dead = SnapshotReader(
        project, url="http://127.0.0.1:8769", service_pid=42,
        probe=lambda pid: False, transport=httpx.MockTransport(fail),
    ).collect(now_ms=5000)
    incidents = opened(Watchdog().observe(dead))
    assert {"service_unreachable", "service_process_exited"} <= incidents.keys()
    replaced = SnapshotReader(
        project, service_pid=42, service_start_ms=100,
        probe=lambda pid: {"alive": True, "started_at_ms": 200, "executable": "python.exe"},
    ).collect(now_ms=5000)
    incidents = opened(Watchdog().observe(replaced))
    assert "service_process_replaced" in incidents
    assert "service_process_exited" not in incidents


def test_remote_service_url_is_rejected_before_any_probe(project):
    with pytest.raises(ValueError, match="loopback"):
        SnapshotReader(project, url="https://example.com")


def test_process_identity_can_observe_this_process_without_control():
    result = process_identity(os.getpid())
    assert result["alive"] is True
    assert result["started_at_ms"] > 0
    assert result["executable"]


def test_cli_once_emits_jsonl_and_atomically_replaces_latest_snapshot(tmp_path, capsys):
    log = tmp_path / "monitoring" / "incidents.jsonl"
    latest = tmp_path / "monitoring" / "latest-status.json"
    code = main(["--project", str(tmp_path), "--once", "--jsonl", str(log),
                 "--latest", str(latest)])
    assert code == 2
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["type"] == "snapshot" and records[0]["read_only"]
    assert records[1]["code"] == "database_unavailable"
    assert [json.loads(line) for line in log.read_text().splitlines()] == records
    assert json.loads(latest.read_text()) == records[0]
    assert list(latest.parent.glob(".snapshot-*")) == []


def test_incidents_resolve_once_after_service_returns():
    watchdog = Watchdog()
    current = observation(10_000)
    current["service"]["reachable"] = False
    assert "service_unreachable" in opened(watchdog.observe(current))
    current["observed_at_ms"] += 1000
    assert "service_unreachable" not in opened(watchdog.observe(current))
    current["service"]["reachable"] = True
    current["observed_at_ms"] += 1000
    result = watchdog.observe(current)
    assert sum(row.get("event") == "resolved" and row.get("code") == "service_unreachable"
               for row in result) == 1


def test_loss_of_database_telemetry_does_not_falsely_resolve_a_stalled_turn():
    watchdog = Watchdog()
    watchdog.observe(observation())
    unavailable = observation(610_000)
    unavailable["database"] = {"available": False, "error": "database_read_failed"}
    records = watchdog.observe(unavailable)
    assert not any(row.get("event") == "resolved" and row.get("code") == "turn_progress_stalled"
                   for row in records)
    assert "turn_progress_stalled" in {
        row["code"] for row in records[0]["active_incidents"]
    }


def test_terminal_run_preserves_state_with_large_usage():
    current = observation()
    current["database"]["run"].update(state="stopped", tokens_used=10_000_000, turns_used=100_000)
    current["database"]["turns"] = []
    current["database"]["workers"] = []
    current["database"]["attempts"] = []
    current["service"]["run"]["driving"] = False
    records = Watchdog().observe(current)
    assert records[0]["phase"] == "stopped"
    assert not any(code.startswith("budget_hold") for code in opened(records))


def test_snapshot_omits_candidate_bodies_and_bounds_driver_outcomes(project):
    def respond(request):
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={"armed_runs": ["run"], "running_drivers": []})
        return httpx.Response(200, json={
            "id": "run", "state": "running", "driving": False, "armed": True,
            "driver_result": {"state": "finished", "result": {
                "run_id": "run", "state": "idle", "steps": 20,
                "results": [{"task_id": str(number), "directive": "verify",
                             "validation": {"data": {"body": "Private candidate content"}}}
                            for number in range(20)],
            }},
        })

    result = SnapshotReader(
        project, url="http://127.0.0.1:8769", run_id="run",
        transport=httpx.MockTransport(respond),
    ).collect(now_ms=5000)
    summary = result["service"]["run"]["driver_result"]["result"]
    assert len(summary["recent_outcomes"]) == 8
    assert "Private candidate content" not in json.dumps(result)


def runtime_record(event, stamp, **fields):
    return {
        "timestamp": datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        "event": event, "run_id": "run", "worker_id": "worker",
        "attempt_id": "attempt", "task_id": "task", **fields,
    }


def append_runtime(path, *records):
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def test_runtime_tail_waits_for_complete_lines_and_tracks_tools(tmp_path):
    path = tmp_path / "runtime-events.jsonl"
    tail = RuntimeTail(path)
    assert tail.read("run", 10_000)["available"] is False
    append_runtime(path, runtime_record("activation_started", 1000),
                   runtime_record("tool_started", 2000, tool="taskhub_read"))
    pending = json.dumps(runtime_record("tool_finished", 3000, tool="taskhub_read", status="ok"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(pending[:40])
    first = tail.read("run", 10_000)
    assert first["partial_bytes"] == 40
    assert first["workers"][0]["active_tools"]["taskhub_read"]["count"] == 1
    with path.open("a", encoding="utf-8") as stream:
        stream.write(pending[40:] + "\n")
    second = tail.read("run", 10_000)
    assert second["invalid_records"] == 0
    assert second["workers"][0]["active_tools"] == {}
    assert second["workers"][0]["last_tool_result"]["status"] == "ok"
    assert len(second["recent_events"]) == 3


def test_runtime_tail_is_bounded_filters_content_and_detects_reset(tmp_path):
    path = tmp_path / "runtime-events.jsonl"
    tail = RuntimeTail(path, max_bytes=1024)
    append_runtime(path, *(runtime_record("model_activity", 1000 + index, method="item/started")
                           for index in range(30)))
    append_runtime(path, runtime_record("model_activity", 2000, method="item/started",
                                       prompt="Private source", raw_output="Private tool output"))
    first = tail.read("run", 10_000)
    assert first["bytes_read"] <= 1024
    assert first["history_incomplete"] and first["tail_skipped"]
    assert "Private" not in json.dumps(first)
    assert first["workers"][0]["last_activity_at_ms"] == 2000
    path.write_text(json.dumps(runtime_record("activation_started", 3000)) + "\n")
    second = tail.read("run", 10_000)
    assert second["reset_detected"]
    assert second["workers"][0]["last_activity_at_ms"] == 3000


def test_runtime_log_progress_prevents_false_stall_without_a_turn_deadline(project):
    path = project / ".task-based" / "runtime-events.jsonl"
    append_runtime(path, runtime_record("activation_started", 1000),
                   runtime_record("turn_started", 2000, turn_id="remote-turn"),
                   runtime_record("model_activity", 599_000, method="item/started"))
    reader = SnapshotReader(project, run_id="run")
    first = reader.collect(now_ms=600_000)
    assert first["database"]["workers"][0]["last_activity_at"] == 599_000
    watchdog = Watchdog()
    incidents = opened(watchdog.observe(first))
    assert "turn_progress_stalled" not in incidents
    assert "turn_overdue" not in incidents
    later = reader.collect(now_ms=901_000)
    incidents = opened(watchdog.observe(later))
    assert "turn_progress_stalled" in incidents


def test_runtime_progress_cannot_cross_run_or_attempt_boundary(project):
    path = project / ".task-based" / "runtime-events.jsonl"
    append_runtime(path,
                   runtime_record("model_activity", 599_000, run_id="other-run"),
                   runtime_record("model_activity", 599_000, attempt_id="old-attempt"))
    snapshot = SnapshotReader(project, run_id="run").collect(now_ms=600_000)
    worker = snapshot["database"]["workers"][0]
    assert "runtime_activity" not in worker
    assert worker["last_activity_at"] == 1000
    assert "turn_progress_stalled" in opened(Watchdog().observe(snapshot))


def test_missing_and_malformed_runtime_telemetry_remain_explicit(project):
    reader = SnapshotReader(project, run_id="run")
    missing = reader.collect(now_ms=600_000)
    assert "runtime_telemetry_unavailable" in opened(Watchdog().observe(missing))
    path = project / ".task-based" / "runtime-events.jsonl"
    path.write_text("not-json\n")
    append_runtime(path, runtime_record("model_activity", 599_000, method="item/started"))
    snapshot = reader.collect(now_ms=600_000)
    assert snapshot["runtime_telemetry"]["invalid_records"] == 1
    incidents = opened(Watchdog().observe(snapshot))
    assert "runtime_telemetry_degraded" in incidents
    assert "turn_progress_stalled" not in incidents


def historical_budget_stop_observation():
    current = observation()
    current["database"]["run"]["limits"].update(max_turns=10, token_allowance=1000)
    current["database"]["run"].update(state="paused", tokens_used=1000)
    current["database"]["attempts"] = []
    current["database"]["turns"] = []
    current["database"]["workers"] = []
    raw = {"state": "finished", "result": {
        "state": "needs_attention", "stop_reason": "budget_hold",
        "results": [{
            "task_id": "task", "worker_id": "worker", "turn_status": "interrupted",
            "directive": "parked", "stop_reason": "budget_hold", "process_quiet": True,
            "error": None, "diagnostic_refs": [],
        }],
    }}
    return current, raw


def test_historical_budget_stop_stays_visible_without_suppressing_attention():
    current, raw = historical_budget_stop_observation()
    current["service"]["run"]["driver_result"] = _driver_summary(raw)
    current["service"]["run"]["driving"] = False
    records = Watchdog().observe(current)
    snapshot = records[0]
    assert "expected_budget_stop" not in snapshot
    assert snapshot["phase"] == "needs_attention"
    assert snapshot["run"]["state"] == "paused"
    assert snapshot["service"]["run"]["driver_result"]["result"]["stop_reason"] == "budget_hold"
    assert not any(code.startswith("budget_hold") for code in opened(records))
    assert {"driver_needs_attention", "run_paused"} <= opened(records).keys()


@pytest.mark.parametrize("failure", [
    "legacy_reason_missing", "error", "diagnostic", "unknown_turn", "uncertain_usage",
    "active_attempt", "not_quiet", "earlier_failure_outside_summary_window",
])
def test_historical_stop_reason_never_masks_missing_evidence_or_actual_failure(failure):
    current, raw = historical_budget_stop_observation()
    outcome = raw["result"]["results"][-1]
    if failure == "legacy_reason_missing":
        outcome.pop("stop_reason")
        raw["result"].pop("stop_reason")
    elif failure == "error":
        outcome["error"] = "Independent runtime failure"
    elif failure == "diagnostic":
        outcome["diagnostic_refs"] = ["a" * 64]
    elif failure == "unknown_turn":
        outcome["turn_status"] = "unknown"
    elif failure == "uncertain_usage":
        current["database"]["run"]["usage_uncertain"] = 1
    elif failure == "active_attempt":
        current["database"]["attempts"] = [{
            "id": "active", "state": "running",
        }]
    elif failure == "not_quiet":
        outcome["process_quiet"] = False
    else:
        raw["result"]["results"] = [
            {"turn_status": "failed", "error": "Earlier failure"},
            *[{"turn_status": "completed"} for _ in range(10)],
            outcome,
        ]
    current["service"]["run"]["driver_result"] = _driver_summary(raw)
    records = Watchdog().observe(current)
    assert "expected_budget_stop" not in records[0]
    assert "driver_needs_attention" in opened(records)


def test_driver_diagnostic_references_are_bounded_and_preserved():
    refs = [f"{number:064x}" for number in range(20)]
    raw = {"state": "finished", "result": {
        "state": "needs_attention",
        "results": [{"turn_status": "failed", "diagnostic_refs": refs, "error": "Failure"}],
    }}
    result = _driver_summary(raw)["result"]
    assert result["has_failure_evidence"]
    assert result["recent_outcomes"][0]["diagnostic_refs"] == refs[-8:]


@pytest.mark.parametrize("error_type", ["transport_error", "rate_limit", "usage_limit"])
def test_runtime_error_event_keeps_diagnostic_reference_without_raw_content(project, error_type):
    path = project / ".task-based" / "runtime-events.jsonl"
    append_runtime(path, runtime_record(
        "runtime_error", 599_000, status="error", error_type=error_type,
        diagnostic_sha256="b" * 64, raw_message="Sensitive raw diagnostic",
    ))
    snapshot = SnapshotReader(project, run_id="run").collect(now_ms=600_000)
    assert snapshot["runtime_telemetry"]["invalid_records"] == 0
    assert "Sensitive raw diagnostic" not in json.dumps(snapshot)
    incident = opened(Watchdog().observe(snapshot))["runtime_error"]
    assert incident["details"]["diagnostic_sha256"] == "b" * 64
    assert incident["details"]["error_type"] == error_type
    assert incident["category"] == "runtime_failure"


def test_draining_stall_is_visible_with_no_live_turn():
    current = observation(10_000)
    current["database"]["turns"] = []
    current["database"]["workers"][0]["state"] = "draining"
    current["database"]["attempts"][0]["state"] = "draining"
    current["database"]["tasks"] = [{"id": "helper", "state": "ready", "actionable": True}]
    watchdog = Watchdog(MonitorPolicy(stage_seconds=60))
    assert "worker_stage_stalled" not in opened(watchdog.observe(current))
    current["observed_at_ms"] = 71_000
    records = watchdog.observe(current)
    assert opened(records)["worker_stage_stalled"]["details"]["stage"] == "draining"
    assert records[0]["phase"] == "needs_attention"
    current["database"]["workers"][0].update(state="idle", active_attempt=None)
    current["database"]["attempts"] = []
    assert any(row.get("event") == "resolved" and row.get("code") == "worker_stage_stalled"
               for row in watchdog.observe(current))


def test_known_preparation_start_can_expose_a_pre_turn_stall():
    current = observation(100_000)
    current["database"]["turns"] = []
    worker = current["database"]["workers"][0]
    worker["state"] = "preparing"
    worker["runtime_activity"] = {"activation_started_at_ms": 1000}
    incidents = opened(Watchdog(MonitorPolicy(stage_seconds=60)).observe(current))
    assert incidents["worker_stage_stalled"]["details"]["stage"] == "preparing"


def test_bounded_long_validation_is_attention_not_an_invented_failure():
    current = observation()
    current["database"]["turns"] = []
    current["database"]["attempts"] = []
    current["database"]["workers"] = []
    current["database"]["tasks"] = [{
        "id": "validating", "state": "verifying", "acceptance_policy": "general.json@1",
        "latest_validation": {"id": "candidate", "created_at": 1000, "verdict": None},
    }]
    incidents = opened(Watchdog().observe(current))
    assert incidents["validation_pending"]["severity"] == "warning"
    assert "worker_stage_stalled" not in incidents
    assert "driver_needs_attention" not in incidents


def test_blocked_provider_reasons_and_wait_cycles_are_visible_without_mutation(project):
    path = project / ".task-based" / "state.sqlite3"
    helper = TaskContract(
        title="Helper record", objective="Review a software prerequisite",
        requirements=[{
            "key": "parent", "task_id": "task", "task_revision": 1, "policy": "general.manual@1",
        }],
    )
    predicate = {"mode": "all", "requirements": [{
        "key": "helper", "task_id": "helper", "task_revision": 1, "policy": "general.manual@1",
    }]}
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE tasks SET state='waiting',active_attempt=NULL WHERE id='task'")
        connection.execute("UPDATE workers SET state='suspended',active_attempt=NULL")
        connection.execute("UPDATE attempts SET ended_at=2000,state='suspended'")
        connection.execute("UPDATE turn_intents SET state='completed'")
        connection.execute(
            "INSERT INTO tasks(id,current_revision,state,created_at) VALUES('helper',1,'ready',2000)"
        )
        connection.execute("INSERT INTO task_revisions VALUES(?,?,?,?)", (
            "helper", 1, helper.model_dump_json(), "helper-hash",
        ))
        connection.execute(
            "INSERT INTO continuations(id,task_id,task_revision,preferred_worker,predicate_json,"
            "checkpoint_json,state,origin_attempt,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("continuation", "task", 1, "worker", json.dumps(predicate), "{}", "waiting", "attempt", 2000),
        )
    database = read_database(project, "run")
    assert database["dependency_cycles"] == [["helper", "task"]]
    blocked = next(row for row in database["tasks"] if row["id"] == "helper")
    assert not blocked["actionable"]
    assert blocked["blocked_reasons"][0]["code"] == "provider_receipt_pending"
    current = observation()
    current["database"] = database
    current["service"]["run"].update(driving=False, driver_result={"state": "finished",
                                                                "result": {"state": "idle"}})
    incidents = opened(Watchdog().observe(current))
    assert {"dependency_blocked", "dependency_cycle"} <= incidents.keys()
    assert incidents["dependency_cycle"]["severity"] == "warning"


def test_missing_input_artifacts_are_explicit_in_blocked_task_summary(project):
    path = project / ".task-based" / "state.sqlite3"
    contract = TaskContract(title="Software input", objective="Use a software input",
                            inputs=["missing-input"])
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE tasks SET state='ready',active_attempt=NULL WHERE id='task'")
        connection.execute("UPDATE task_revisions SET contract_json=? WHERE task_id='task'",
                           (contract.model_dump_json(),))
    task = read_database(project, "run")["tasks"][0]
    assert task["blocked_reasons"] == [{"code": "input_missing", "artifact_id": "missing-input"}]
    assert not task["actionable"]


def test_a_requirement_on_a_rejected_artifact_is_met_by_its_accepted_correction(project):
    path = project / ".task-based" / "state.sqlite3"
    contract = TaskContract(title="Software requirement", objective="Use a corrected software result",
                            requirements=[{"key": "result", "artifact_id": "first", "policy": "general.manual@1"}])
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE tasks SET state='ready',active_attempt=NULL WHERE id='task'")
        connection.execute("UPDATE task_revisions SET contract_json=? WHERE task_id='task'",
                           (contract.model_dump_json(),))
        connection.execute("INSERT INTO artifacts(id,kind,body_sha,status,created_at) VALUES"
                           "('first','software.result','a','rejected',1000),('second','software.result','b','accepted',1001)")
        connection.execute("INSERT INTO evidence(id,artifact_id,policy,verdict,details_json,qualifications_json,"
                           "issuer,created_at) VALUES('evidence','second','general.manual@1','accepted','{}','[]','validator',1002)")
    assert read_database(project, "run")["tasks"][0]["blocked_reasons"][0]["artifact_id"] == "first"
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO artifact_obligations(artifact_id,task_id,predecessor_id) VALUES('second','task','first')")
    assert read_database(project, "run")["tasks"][0]["blocked_reasons"] == []



def add_parked_interruption(project, stop_reason="operator_pause"):
    profile = load_config(project).codex.model_dump(mode="json")
    snapshot = "d" * 64
    blob = project / ".task-based" / "objects" / "sha256" / "dd" / snapshot
    blob.parent.mkdir(parents=True)
    blob.write_text("{}")
    workspace = project / ".task-based" / "workers" / "worker" / "work"
    workspace.mkdir(parents=True)
    with sqlite3.connect(project / ".task-based" / "state.sqlite3") as connection:
        connection.execute("UPDATE tasks SET state='waiting',active_attempt=NULL")
        connection.execute(
            "UPDATE workers SET state='suspended',active_attempt=NULL,thread_id='thread',"
            "profile_json=?", (json.dumps({"codex_profile": profile}),),
        )
        connection.execute("UPDATE attempts SET state='interrupted',ended_at=2000")
        connection.execute("UPDATE turn_intents SET state='interrupted'")
        connection.execute(
            "INSERT INTO interruption_checkpoints("
            "id,task_id,task_revision,worker_id,origin_attempt,turn_intent_id,thread_id,"
            "usage_epoch,workspace,codex_profile_json,snapshot,input_refs_json,checkpoint_json,"
            "proof_json,stop_reason,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ticket", "task", 1, "worker", "attempt", "turn", "thread", 0,
             str(workspace.resolve()), json.dumps(profile), snapshot, "[]", "{}", "{}",
             stop_reason, "parked", 2000),
        )
    return snapshot, workspace


def test_a_ticket_parked_in_an_earlier_run_is_resume_work_without_hashing(project, monkeypatch):
    add_parked_interruption(project)
    with sqlite3.connect(project / ".task-based" / "state.sqlite3") as connection:
        connection.execute("UPDATE runs SET state='paused',tokens_used=1000")
        connection.execute("INSERT INTO runs(id,state,limits_json,created_at) VALUES(?,?,?,?)", (
            "new-run", "running", Limits().model_dump_json(), 3000,
        ))

    def forbidden(*args, **kwargs):
        raise AssertionError("The monitor must never hash a workspace")

    monkeypatch.setattr("formal_geyser.coordination.storage.BlobStore.snapshot", forbidden)
    database = read_database(project, "new-run")
    assert database["available"]
    assert database["workers"] == []
    ticket = database["interruptions"]["tickets"][0]
    assert ticket["readiness"] == "candidate"
    assert ticket["source_run_id"] == "run"
    assert ticket["stop_reason"] == "operator_pause"
    assert ticket["snapshot_content_check"] == "deferred_to_runtime"
    current = observation()
    current["database"] = database
    current["service"]["run"].update(id="new-run", driving=False, driver_result=None)
    records = Watchdog(MonitorPolicy(idle_grace_seconds=0)).observe(current)
    assert records[0]["interruption_candidates"] == 1
    assert opened(records)["idle_actionable_work"]["details"]["interruption_candidates"] == 1


@pytest.mark.parametrize(("change", "reason"), [
    ("profile", "profile_changed"),
    ("worker", "worker_or_task_unavailable"),
    ("session", "session_identity_changed"),
    ("input", "input_withdrawn"),
    ("snapshot", "snapshot_reference_missing"),
    ("workspace", "workspace_identity_changed"),
])
def test_parked_ticket_blockers_are_operator_visible(project, change, reason):
    snapshot, workspace = add_parked_interruption(project)
    with sqlite3.connect(project / ".task-based" / "state.sqlite3") as connection:
        if change == "profile":
            profile = load_config(project).codex.model_dump(mode="json")
            profile["model"] = "different"
            connection.execute("UPDATE interruption_checkpoints SET codex_profile_json=?",
                               (json.dumps(profile),))
        elif change == "worker":
            connection.execute("UPDATE workers SET state='idle'")
        elif change == "session":
            connection.execute("UPDATE workers SET thread_id='different'")
        elif change == "input":
            connection.execute(
                "INSERT INTO artifacts(id,kind,body_sha,status,created_at) VALUES(?,?,?,?,?)",
                ("input", "software.source", "a" * 64, "withdrawn", 1000),
            )
            connection.execute("UPDATE attempts SET input_refs_json=?",
                               (json.dumps(["input"]),))
            connection.execute("UPDATE interruption_checkpoints SET input_refs_json=?",
                               (json.dumps(["input"]),))
        elif change == "snapshot":
            (project / ".task-based" / "objects" / "sha256" / "dd" / snapshot).unlink()
        elif change == "workspace":
            connection.execute("UPDATE interruption_checkpoints SET workspace=?",
                               (str(workspace.parent / "other"),))
    database = read_database(project, "run")
    ticket = database["interruptions"]["tickets"][0]
    assert ticket["readiness"] == "blocked"
    assert reason in ticket["blocked_reasons"]
    current = observation()
    current["database"] = database
    current["service"]["run"].update(driving=False, driver_result=None)
    records = Watchdog(MonitorPolicy(idle_grace_seconds=0)).observe(current)
    assert records[0]["interruption_candidates"] == 0
    assert "interruption_blocked" in opened(records)


def test_accounting_warning_does_not_block_a_parked_continuation(project):
    add_parked_interruption(project)
    with sqlite3.connect(project / ".task-based" / "state.sqlite3") as connection:
        connection.execute("UPDATE runs SET usage_uncertain=1")
        connection.execute("INSERT INTO worker_usage_holds(worker_id,run_id,reason,created_at) "
                           "SELECT worker_id,run_id,'Unresolved source execution usage',0 FROM attempts LIMIT 1")
    ticket = read_database(project, "run")["interruptions"]["tickets"][0]
    assert ticket["readiness"] == "candidate"
    assert ticket["accounting_uncertain"] is True
    assert ticket["blocked_reasons"] == []


def test_explicit_snapshot_failure_overrides_provisional_ticket_readiness(project):
    add_parked_interruption(project)
    current = observation()
    current["database"] = read_database(project, "run")
    current["service"]["run"].update(driving=False, driver_result={
        "state": "failed", "error": {"code": "SNAPSHOT_CHANGED", "message": "Original workspace changed"},
    })
    records = Watchdog(MonitorPolicy(idle_grace_seconds=0)).observe(current)
    assert records[0]["interruption_metadata_candidates"] == 1
    assert records[0]["interruption_candidates"] == 0
    assert records[0]["interruption_resume_error"] == "SNAPSHOT_CHANGED"
    incidents = opened(records)
    assert {"interruption_resume_failed", "driver_needs_attention"} <= incidents.keys()
    assert "idle_actionable_work" not in incidents


def test_workspace_content_verification_is_deferred_to_runtime(project):
    _, workspace = add_parked_interruption(project)
    (workspace / "changed.txt").write_text("Changed software record")
    ticket = read_database(project, "run")["interruptions"]["tickets"][0]
    assert ticket["readiness"] == "candidate"
    assert ticket["snapshot_content_check"] == "deferred_to_runtime"
    assert ticket["blocked_reasons"] == []



@pytest.mark.parametrize("recorded_limits", [
    {},
    {"token_allowance": None, "max_turns": None},
    {"token_allowance": None, "max_turns": 10},
    {"token_allowance": 1000, "max_turns": None},
    {"token_allowance": 1000, "max_turns": 10},
    {"token_allowance": "retired", "max_turns": "retired"},
])
def test_usage_totals_never_create_holds_from_retired_limit_metadata(recorded_limits):
    current = observation(10_000)
    run = current["database"]["run"]
    run["limits"].update(recorded_limits)
    run.update(tokens_used=10_000_000, turns_used=100_000)
    records = Watchdog().observe(current)
    snapshot = records[0]
    assert snapshot["database_available"]
    assert snapshot["run"]["limits"] == run["limits"]
    assert snapshot["run"]["tokens_used"] == 10_000_000
    assert snapshot["run"]["turns_used"] == 100_000
    assert not any(code.startswith("budget_hold") for code in opened(records))
    assert "expected_budget_stop" not in snapshot
    assert snapshot["phase"] == "running"


@pytest.mark.parametrize("usage_uncertain", [False, True])
def test_large_usage_preserves_stall_and_accounting_checks_without_a_deadline(usage_uncertain):
    current = observation()
    current["database"]["run"].update(
        tokens_used=10_000_000, turns_used=100_000, usage_uncertain=usage_uncertain,
    )
    incidents = opened(Watchdog().observe(current))
    assert "turn_progress_stalled" in incidents
    assert "turn_overdue" not in incidents
    assert ("usage_uncertain" in incidents) is usage_uncertain
    assert not any(code.startswith("budget_hold") for code in incidents)


@pytest.mark.parametrize("failure_observed", [True, False, None])
def test_truncated_driver_results_preserve_aggregate_failure_accounting(failure_observed):
    current = observation(10_000)
    raw = {"state": "finished", "result": {
        "state": "idle", "results": [{"turn_status": "completed"} for _ in range(100)],
        "steps": 101, "results_retained": 100, "results_truncated": 1,
    }}
    if failure_observed is not None:
        raw["result"]["failure_observed"] = failure_observed
    driver = _driver_summary(raw)
    summary = driver["result"]
    assert summary["steps"] == 101
    assert summary["results_retained"] == 100
    assert summary["results_truncated"] == 1
    assert summary["failure_observed"] is failure_observed
    assert len(summary["recent_outcomes"]) == 8
    assert all(not row["has_failure_evidence"] for row in summary["recent_outcomes"])
    assert summary["has_failure_evidence"] is (failure_observed is True)
    current["service"]["run"].update(driving=False, driver_result=driver)
    records = Watchdog().observe(current)
    assert "expected_budget_stop" not in records[0]
    assert ("driver_needs_attention" in opened(records)) is (failure_observed is True)
