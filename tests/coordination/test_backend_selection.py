"""Each worker keeps one backend for life; accounts are held and charged per backend."""
from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination import faults
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.faults import ACCOUNT_HOLDS, HOLD_MARKERS
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import claude as claude_runtime
from tests.coordination.lifecycle_helpers import (
    assignment_decision,
    idle_worker,
)


async def command(hub, name, payload=None, *, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), Actor(role=role))


@pytest.fixture
async def project(tmp_path, monkeypatch):
    # Selecting Claude checks this machine; the tests exercise the hub, not the platform.
    monkeypatch.setattr(claude_runtime, "claude_unavailable", lambda config: None)
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    await command(hub, "task.publish", {"contract": {"title": "Continue the proof", "objective": "Inspect the workspace."}})
    run = await command(hub, "run.start")
    try:
        yield hub, run
    finally:
        await hub.close()


async def test_new_workers_follow_the_switch_and_a_chosen_worker_keeps_its_backend(project):
    hub, run = project
    assert (await hub.backend_status())["new_worker_backend"] == "codex"
    codex = await idle_worker(hub, run, "Earlier lemma")
    assert codex["backend"] == "codex"
    with pytest.raises(HubError) as refused:
        await command(hub, "runtime.select_backend", {"backend": "claude"}, role=Role.RUNNER)
    assert refused.value.code != "INVALID_INPUT"
    status = await command(hub, "runtime.select_backend", {"backend": "claude"})
    assert status["new_worker_backend"] == "claude"
    claude = await command(hub, "worker.register")
    assert claude["backend"] == "claude"
    # A worker chosen for a new task starts it in a new conversation, on its own account.
    intent = await assignment_decision(hub, run["id"])
    assert [item["worker_id"] for item in intent["candidates"]["workers"]] == [codex["id"], claude["id"]]
    chosen = await hub.complete_assignment(intent["id"], codex["display_name"])
    assert chosen["outcome"] == "idle_worker" and chosen["assignment"]["worker_id"] == codex["id"]
    worker = await hub.worker(codex["id"])
    assert worker["backend"] == "codex" and worker["thread_id"] is None
    assert (await hub.backend_status())["workers"] == {"codex": 1, "claude": 1}
    with pytest.raises(HubError) as invalid:
        await command(hub, "runtime.select_backend", {"backend": "gpt"})
    assert invalid.value.code == "INVALID_INPUT"


async def test_selecting_claude_reports_why_this_machine_cannot_run_it(project, monkeypatch):
    hub, _ = project
    monkeypatch.setattr(claude_runtime, "claude_unavailable", lambda config: "Claude needs Linux here")
    with pytest.raises(HubError) as refused:
        await command(hub, "runtime.select_backend", {"backend": "claude"})
    assert refused.value.code == "BACKEND_UNAVAILABLE" and "Linux" in refused.value.message
    assert (await hub.backend_status())["new_worker_backend"] == "codex"


async def test_a_held_claude_subscription_stops_only_claude_workers(project):
    hub, run = project
    await command(hub, "runtime.select_backend", {"backend": "claude"})
    assert await hub.hold_dispatch("claudeUsageLimit", "Limit reached", subject_id="turn") is True
    # No new worker starts on the held Claude account: the open task waits.
    assert await hub.next_assignment(run["id"]) is None
    assert (await hub.backend_status())["workers"] == {"codex": 0, "claude": 0}
    await command(hub, "runtime.select_backend", {"backend": "codex"})
    assignment = await hub.next_assignment(run["id"])
    assert assignment is not None and (await hub.worker(assignment["worker_id"]))["backend"] == "codex"
    await hub.require_dispatch("codex")
    with pytest.raises(HubError) as held:
        await hub.require_dispatch("claude")
    assert held.value.code == "DISPATCH_HELD"
    record = await hub.run(run["id"], worker_id=assignment["worker_id"])
    assert record["dispatch_hold"] is None
    assert record["dispatch_holds"]["claude"]["message"] == ACCOUNT_HOLDS["claudeUsageLimit"]
    # A Codex account error holds Codex separately; Resume releases both.
    assert await hub.hold_dispatch("usageLimitExceeded", "Codex limit", subject_id="turn") is True
    assert set((await hub.backend_status())["dispatch_holds"]) == {"codex", "claude"}
    await command(hub, "run.resume", {"run_id": run["id"]})
    await hub.finish_dispatch_release()
    assert (await hub.backend_status())["dispatch_holds"] == {}
    assert not (hub.state_dir / HOLD_MARKERS["claude"]).exists()


async def test_an_unsandboxable_machine_holds_both_backends_until_it_is_fixed(project, monkeypatch):
    hub, run = project
    monkeypatch.setattr(faults, "ENVIRONMENT_SECONDS", 0.0)
    blocker = ["WSL interop is on"]
    hub.environment_probe = lambda: blocker[0]
    # Nothing starts, on either backend: the open task waits and no session is abandoned.
    assert await hub.next_assignment(run["id"]) is None
    holds = (await hub.backend_status())["dispatch_holds"]
    assert set(holds) == {"codex", "claude"} and holds["codex"]["code"] == "sandboxUnavailable"
    assert holds["claude"]["message"].startswith("WSL interop is on. New sessions wait")
    with pytest.raises(HubError) as held:
        await hub.require_dispatch("claude")
    assert held.value.code == "DISPATCH_HELD"
    assert (await hub.run(run["id"]))["dispatch_hold"]["code"] == "sandboxUnavailable"
    # Resume cannot release it; fixing the machine does, without Resume.
    await command(hub, "run.resume", {"run_id": run["id"]})
    assert set((await hub.backend_status())["dispatch_holds"]) == {"codex", "claude"}
    blocker[0] = None
    assert (await hub.backend_status())["dispatch_holds"] == {}
    assert await hub.next_assignment(run["id"]) is not None


def test_the_service_checks_the_machines_sandboxing_and_tools_do_not(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from formal_geyser.coordination import native_permissions
    from formal_geyser.coordination.api import create_app

    monkeypatch.setattr(native_permissions, "wsl_interop", lambda: True)
    assert native_permissions.sandbox_blocker() == native_permissions.INTEROP
    monkeypatch.setattr(native_permissions, "wsl_interop", lambda: False)
    assert native_permissions.sandbox_blocker() is None
    initialize(tmp_path, ProjectConfig())
    assert Hub(tmp_path).environment_probe is None
    with TestClient(create_app(tmp_path, environment_holds=True)) as client:
        assert client.app.state.hub.environment_probe is native_permissions.sandbox_blocker


async def test_a_claude_hold_survives_restart_in_its_own_marker(project):
    hub, run = project
    await hub.hold_dispatch("claudeSignInRequired", "Not signed in", subject_id="turn")
    assert (hub.state_dir / HOLD_MARKERS["claude"]).exists()
    assert not (hub.state_dir / HOLD_MARKERS["codex"]).exists()
    await hub.close()
    reopened = Hub(hub.root)
    try:
        holds = (await reopened.backend_status())["dispatch_holds"]
        assert set(holds) == {"claude"} and holds["claude"]["code"] == "claudeSignInRequired"
        await reopened.require_dispatch("codex")
    finally:
        await reopened.close()


async def test_usage_is_counted_per_backend_account(project):
    hub, run = project
    assignment = await hub.next_assignment(run["id"])
    await command(hub, "usage.record", {"worker_id": assignment["worker_id"], "sample": {"totalTokens": 100}},
                  role=Role.RUNNER)
    await command(hub, "runtime.select_backend", {"backend": "claude"})
    claude = await command(hub, "worker.register")
    await hub.db.call(lambda c: c.execute("UPDATE workers SET run_id=? WHERE id=?", (run["id"], claude["id"])),
                      write=True)
    charged = await command(hub, "usage.record", {"worker_id": claude["id"], "sample": {"totalTokens": 40}},
                            role=Role.RUNNER)
    assert charged["backend"] == "claude" and charged["backend_tokens"] == 40
    record = await hub.run(run["id"])
    assert (record["tokens_used"], record["codex_tokens_used"], record["claude_tokens_used"]) == (140, 100, 40)
    assert (await hub.backend_status())["tokens_used"] == {"codex": 100, "claude": 40}
