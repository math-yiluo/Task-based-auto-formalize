"""Busy scheduling rows do not trigger native-file or environment verification."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser import bootstrap_runtime
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, Role
from tests.coordination.lifecycle_helpers import (
    open_collaboration,
    resolve_recruitment,
    select_review,
    validate_candidate,
)
from tests.coordination.publication_helpers import file_payload, new_hub


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             actor or Actor(role=Role.OPERATOR))


def worker(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


@pytest.fixture
async def scan_case(tmp_path):
    hubs = []
    async def create(*, assigned=True):
        hub = await new_hub(tmp_path / str(len(hubs)))
        hubs.append(hub)
        task = await command(hub, "task.publish", {"contract": {
            "title": "Scheduler fixture", "objective": "Inspect one exact native work product."}})
        run = await command(hub, "run.start")
        author = await hub.next_assignment(run["id"])
        candidate = await command(hub, "artifact.stage", file_payload(hub, author,
            data={"result": "Synthetic source only."}), worker(author))
        await validate_candidate(hub, candidate["artifact_id"])  # Its review opens once validated.
        reviewer = await select_review(hub, candidate["review_task_id"], run["id"]) if assigned else None
        return SimpleNamespace(hub=hub, task=task, run=run, author=author, candidate=candidate, reviewer=reviewer)
    yield create
    for hub in hubs:
        await hub.close()


def forbid_full_checks(case, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("An unavailable opportunity reached full native validation")
    monkeypatch.setattr(case.hub, "_task", forbidden)
    monkeypatch.setattr(case.hub, "_valid_inputs", forbidden)
    monkeypatch.setattr(case.hub, "_basis_valid", forbidden)
    monkeypatch.setattr(bootstrap_runtime, "require_contract_environment", forbidden)


async def test_all_busy_opportunities_and_saved_work_scans_use_only_cheap_rows(scan_case, monkeypatch):
    case = await scan_case()
    forbid_full_checks(case, monkeypatch)
    assert await case.hub.db.call(lambda c: case.hub._opportunities(c)) == []
    assert await case.hub.next_assignment(case.run["id"]) is None


async def test_recoverable_reviewer_ownership_excludes_the_review_before_basis_checks(scan_case, monkeypatch):
    case = await scan_case()
    def hold(c):
        c.execute("UPDATE attempts SET state='suspended',ended_at=? WHERE id=?", (case.hub.clock(), case.reviewer["id"]))
        c.execute("UPDATE workers SET active_attempt=NULL,state='suspended' WHERE id=?", (case.reviewer["worker_id"],))
        c.execute("UPDATE task_members SET runnable=0 WHERE worker_id=?", (case.reviewer["worker_id"],))
    await case.hub.db.call(hold, write=True)
    forbid_full_checks(case, monkeypatch)
    assert await case.hub.db.call(lambda c: case.hub._opportunities(c)) == []
    assert await case.hub.next_assignment(case.run["id"]) is None


async def test_an_open_collaborator_place_is_an_opportunity(scan_case):
    case = await scan_case()
    await open_collaboration(case.hub, case.task["id"])
    opportunities = await case.hub.db.call(lambda c: case.hub._opportunities(c))
    assert [(item["task_id"], item["opportunity"]) for item in opportunities] == [(case.task["id"], "collaborator")]


async def test_eligible_review_still_requires_current_lean_environment(scan_case, monkeypatch):
    case = await scan_case(assigned=False)
    expected = case.candidate["review_task_id"]
    assert [item["task_id"] for item in await case.hub.db.call(lambda c: case.hub._opportunities(c))] == [expected]
    # This fixture intentionally has no initialized Lean installation. Switching
    # only its configured domain exercises the real fail-closed readiness gate.
    case.hub.config = case.hub.config.model_copy(update={"domain": "lean"})
    calls = []
    original = bootstrap_runtime.require_contract_environment
    def check(hub, c, contract):
        calls.append(contract["kind"])
        return original(hub, c, contract)
    monkeypatch.setattr(bootstrap_runtime, "require_contract_environment", check)
    assert await case.hub.db.call(lambda c: case.hub._opportunities(c)) == []
    assert calls == ["review"]


async def test_eligible_review_still_requires_valid_inputs(scan_case, monkeypatch):
    case = await scan_case(assigned=False)
    calls = []
    def unavailable(c, contract):
        calls.append(contract["kind"])
        raise HubError("INPUTS_CHANGED", "Synthetic withdrawn prerequisite")
    monkeypatch.setattr(case.hub, "_valid_inputs", unavailable)
    assert await case.hub.db.call(lambda c: case.hub._opportunities(c)) == []
    assert calls == ["review"]


async def test_quiet_saved_executor_still_receives_its_own_task(scan_case):
    case = await scan_case()
    await case.hub.ordinary_turn_completed(case.author["worker_id"], case.author["id"])
    await resolve_recruitment(case.hub, case.run["id"])
    result = await case.hub.next_assignment(case.run["id"])
    assert result["worker_id"] == case.author["worker_id"]
    assert result["task_id"] == case.task["id"]
