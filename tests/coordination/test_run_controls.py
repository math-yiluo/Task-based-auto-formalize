from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from formal_geyser.coordination.config import initialize, load_config
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    Limits,
    ProjectConfig,
    Role,
)

OP = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)
RETIRED = {"max_active", "max_turns", "token_allowance", "turn_seconds"}


async def command(hub, name, payload, actor=OP):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload), actor)


@pytest.fixture
async def hub(tmp_path):
    initialize(tmp_path, ProjectConfig())
    instance = Hub(tmp_path)
    yield instance
    await instance.close()


async def setup_run(hub, limits=None):
    run = await command(hub, "run.start", {"limits": limits or {}})
    task = await command(hub, "task.publish", {"contract": {
        "title": "Software record", "objective": "Prepare an auditable configuration record.",
    }})
    worker = await command(hub, "worker.register", {})
    worker = await command(hub, "worker.bind_session", {
        "worker_id": worker["id"], "thread_id": "thread-" + uuid4().hex,
        "model": "scripted", "codex_profile": hub.config.codex.model_dump(mode="json"),
    }, RUNNER)
    attempt = await command(hub, "task.claim", {
        "task_id": task["id"], "revision": 1, "worker_id": worker["id"], "run_id": run["id"],
    }, RUNNER)
    return run, task, worker, attempt


def test_only_execution_and_context_controls_remain():
    defaults = Limits()
    assert set(defaults.model_dump()) == {"packet_bytes", "delta_bytes"}
    assert RETIRED.isdisjoint(defaults.model_dump())
    assert RETIRED.isdisjoint(Limits.model_json_schema()["properties"])
    for field in ("packet_bytes", "delta_bytes"):
        with pytest.raises(ValidationError):
            Limits.model_validate({field: None})


@pytest.mark.parametrize("field", sorted(RETIRED))
@pytest.mark.parametrize("value", [None, 1, 200000])
async def test_retired_controls_are_rejected_in_new_requests(hub, field, value):
    with pytest.raises(HubError) as blocked:
        await command(hub, "run.start", {"limits": {field: value}})
    assert blocked.value.code == "INVALID_INPUT"
    assert not await hub.list_runs()


@pytest.mark.parametrize("payload", [{}, {"limits": {}}])
async def test_run_start_needs_no_cumulative_authorization(hub, payload):
    run = await command(hub, "run.start", payload)
    assert run["state"] == "running"
    assert "max_active" not in run["limits"]
    assert RETIRED.isdisjoint(json.loads(run["limits_json"]))


def test_new_config_has_no_retired_limit_and_rejects_one(tmp_path):
    path = initialize(tmp_path, ProjectConfig())
    content = path.read_text(encoding="utf-8")
    assert all(field not in content for field in RETIRED)
    assert RETIRED.isdisjoint(load_config(tmp_path).limits.model_dump())
    path.write_text(content.replace("[limits]", "[limits]\nmax_active = 1"), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(tmp_path)


async def test_usage_counts_never_stop_execution(hub):
    run, _, worker, _ = await setup_run(hub)
    for index in range(16):
        intent = await command(hub, "turn.reserve", {"worker_id": worker["id"]}, RUNNER)
        await command(hub, "turn.bind", {
            "intent_id": intent["id"], "remote_turn_id": "turn-" + str(index),
        }, RUNNER)
        await command(hub, "usage.record", {
            "worker_id": worker["id"], "sample": {"totalTokens": (index + 1) * 100000},
        }, RUNNER)
        await command(hub, "turn.finish", {
            "intent_id": intent["id"], "status": "completed",
        }, RUNNER)
    state = await hub.run(run["id"])
    assert state["state"] == "running"
    assert state["turns_used"] == 16
    assert state["tokens_used"] == 1600000
    assert RETIRED.isdisjoint(state["limits"])
    assert not any(event["kind"] == "run.budget_exhausted" for event in await hub.events())
    packet = await hub.context(worker["id"])
    assert "budget" not in packet
    assert packet["execution"] == {"active_workers": 1}
    assert packet["usage"] == {"turns_used": 16, "tokens_used": 1600000}
    started = next(event for event in await hub.events() if event["kind"] == "run.started")
    assert RETIRED.isdisjoint(started["payload"]["limits"])


async def test_default_allows_independent_workers_without_a_global_cap(hub):
    run, _, _, _ = await setup_run(hub)
    for index in range(2):
        task = await command(hub, "task.publish", {"contract": {
            "title": f"Independent record {index}", "objective": "Prepare independent configuration.",
        }})
        worker = await command(hub, "worker.register", {})
        payload = {"task_id": task["id"], "revision": 1, "worker_id": worker["id"], "run_id": run["id"]}
        assert (await command(hub, "task.claim", payload, RUNNER))["state"] == "running"


async def test_accounting_warning_preserves_the_one_live_invocation_constraint(hub):
    run, _, worker, _ = await setup_run(hub)
    intent = await command(hub, "turn.reserve", {"worker_id": worker["id"]}, RUNNER)
    await command(hub, "turn.finish", {"intent_id": intent["id"], "status": "unknown"}, RUNNER)
    state = await hub.run(run["id"])
    assert state["usage_uncertain"] == 1
    assert state["state"] == "running"
    await command(hub, "run.resume", {"run_id": run["id"]})
    with pytest.raises(HubError) as uncertain:
        await command(hub, "turn.reserve", {"worker_id": worker["id"]}, RUNNER)
    assert uncertain.value.code == "CONFLICT"


@pytest.mark.parametrize("stop_reason", ["operator_pause", "operator_stop"])
async def test_only_current_control_reasons_can_create_interruption_checkpoints(hub, stop_reason):
    run, task, worker, attempt = await setup_run(hub)
    intent = await command(hub, "turn.reserve", {"worker_id": worker["id"]}, RUNNER)
    await command(hub, "turn.bind", {"intent_id": intent["id"], "remote_turn_id": "interrupted-turn"}, RUNNER)
    await command(hub, "usage.record", {"worker_id": worker["id"], "sample": {"totalTokens": 1000000}}, RUNNER)
    if stop_reason == "operator_pause":
        await command(hub, "run.pause", {"run_id": run["id"]})
    else:
        await command(hub, "run.stop", {"run_id": run["id"]})
    await command(hub, "turn.finish", {
        "intent_id": intent["id"], "status": "interrupted", "terminal_observed": True, "process_quiet": True,
    }, RUNNER)
    payload = {
        "attempt_id": attempt["id"], "task_id": task["id"], "task_revision": 1,
        "worker_id": worker["id"], "thread_id": worker["thread_id"],
        "remote_turn_id": "interrupted-turn", "turn_intent_id": intent["id"],
        "usage_epoch": worker["usage_epoch"], "codex_profile": hub.config.codex.model_dump(mode="json"),
        "terminal_observed": True, "process_quiet": True,
        "snapshot": hub.blobs.snapshot(hub.workspace(worker["id"])), "stop_reason": "budget_hold",
    }
    with pytest.raises(HubError) as retired:
        await command(hub, "runtime.park_interrupted", payload, RUNNER)
    assert retired.value.code == "INVALID_INPUT"
    with pytest.raises(HubError):
        await command(hub, "runtime.park_interrupted", {**payload, "stop_reason": "activation_deadline"}, RUNNER)
    parked = await command(hub, "runtime.park_interrupted", {**payload, "stop_reason": stop_reason}, RUNNER)
    assert parked["stop_reason"] == stop_reason


async def test_workers_still_cannot_start_runs(hub):
    with pytest.raises(HubError) as forbidden:
        await command(hub, "run.start", {}, Actor(role=Role.WORKER))
    assert forbidden.value.code == "FORBIDDEN"
