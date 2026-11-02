"""Shared planning graph writes are checked, fenced, atomic, and replayable."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.coordination.runtime import (
    ScriptedAction,
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.entries import scenario_entry
from formal_geyser.coordination.shared_dags import DagFiles, prepare_dag_checker
from tests.coordination.lifecycle_helpers import assignment_decision, idle_worker

from .test_gateway_runtime import call
from .test_gateway_runtime import project as project


def proposed(hub, attempt, *, edges=(), expected=0, folder="edit"):
    source = hub.workspace(attempt["worker_id"]) / f".private/dags/{folder}"
    source.mkdir(parents=True, exist_ok=True)
    definitions = {"nodes": [{"id": name, "title": f"Proposed {name}"} for name in ("D", "E")],
                   "edges": list(edges)}
    (source / "definitions.json").write_text(json.dumps(definitions), encoding="utf-8")
    (source / "proofs.json").write_text('{"nodes": [], "edges": []}', encoding="utf-8")
    return {"source_dir": f".private/dags/{folder}", "expected_revision": expected,
            "summary": "Record the current proposed construction dependencies."}


def current(hub):
    return json.loads((hub.root / "graphs/current.json").read_bytes())


async def test_global_pair_starts_empty_and_save_does_not_publish_or_revise(project):
    hub, task, attempt, runner = project
    first = current(hub)
    assert first["revision"] == 0
    assert json.loads(Path(first["definitions"]).read_bytes()) == {"nodes": [], "edges": []}
    result = await call(runner, attempt, "dag_update", proposed(hub, attempt))
    assert result == current(hub)
    assert result["revision"] == 1 and result["status"] == "planning"
    assert (await hub.task(task["id"]))["revision"] == task["revision"]
    assert await hub.list_artifacts() == []
    assert (await hub.worker(attempt["worker_id"]))["active_attempt"] == attempt["id"]
    manifest = json.loads(Path(result["manifest_path"]).read_bytes())
    assert manifest["author"] == "Worker 0" and manifest["task_id"] == task["id"]


async def test_backend_rejects_cycle_without_trusting_worker_check(project):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt, edges=[{"from": "D", "to": "E"}, {"from": "E", "to": "D"}])
    with pytest.raises(HubError) as error:
        await call(runner, attempt, "dag_update", args)
    assert error.value.code == "INVALID_DAG"
    assert "D" in str(error.value) and "E" in str(error.value)
    assert current(hub)["revision"] == 0


async def test_interleaved_writers_cannot_overwrite_or_create_combined_cycle(project):
    hub, _, attempt, runner = project
    a = proposed(hub, attempt, folder="a", edges=[{"from": "D", "to": "E"}])
    b = proposed(hub, attempt, folder="b", edges=[{"from": "E", "to": "D"}])
    replies = await asyncio.gather(call(runner, attempt, "dag_update", a, "a"),
                                   call(runner, attempt, "dag_update", b, "b"), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in replies) == 1, replies
    # The later writer either finds its revision taken at commit, or, if it began after the first
    # commit, is merged with it, and the merged pair's cycle is refused.
    [refused] = [value for value in replies if not isinstance(value, dict)]
    assert isinstance(refused, HubError) and (refused.code == "DAG_REVISION_CONFLICT" or (
        refused.code == "INVALID_DAG" and refused.message.startswith("Merged with revision 1"))), repr(refused)
    assert current(hub)["revision"] == 1
    merged = proposed(hub, attempt, expected=1, folder="merged",
        edges=[{"from": "D", "to": "E"}, {"from": "E", "to": "D"}])
    with pytest.raises(HubError) as error:
        await call(runner, attempt, "dag_update", merged, "merged")
    assert error.value.code == "INVALID_DAG"
    assert current(hub)["revision"] == 1


async def test_a_stale_writer_whose_merge_makes_a_cycle_is_told_it_was_the_merge(project):
    hub, _, attempt, runner = project
    await call(runner, attempt, "dag_update", proposed(hub, attempt, folder="a", edges=[{"from": "D", "to": "E"}]), "a")
    stale = proposed(hub, attempt, folder="b", edges=[{"from": "E", "to": "D"}])  # Still based on revision 0.
    with pytest.raises(HubError) as error:
        await call(runner, attempt, "dag_update", stale, "b")
    assert error.value.code == "INVALID_DAG"
    assert error.value.message.startswith("Merged with revision 1, another update made since revision 0:")
    assert error.value.details["current_revision"] == 1
    assert current(hub)["revision"] == 1


async def test_replay_returns_original_receipt_without_source_or_pointer_regression(project):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt)
    first = await call(runner, attempt, "dag_update", args, "first")
    second = await call(runner, attempt, "dag_update", {**args, "expected_revision": 1}, "second")
    source = hub.workspace(attempt["worker_id"]) / args["source_dir"]
    (source / "definitions.json").unlink()
    (source / "proofs.json").unlink()
    assert await call(runner, attempt, "dag_update", args, "first") == first
    assert current(hub) == second


async def test_receipt_recovers_navigation_failure_after_commit(project, monkeypatch):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt)
    old = current(hub)
    point = DagFiles.point

    def fail(*_):
        raise OSError("Synthetic graph pointer failure")

    monkeypatch.setattr(DagFiles, "point", fail)
    committed = await call(runner, attempt, "dag_update", args)
    assert committed["revision"] == 1
    assert current(hub) == old
    (hub.workspace(attempt["worker_id"]) / args["source_dir"] / "definitions.json").unlink()
    monkeypatch.setattr(DagFiles, "point", point)
    result = await call(runner, attempt, "dag_update", args)
    assert current(hub) == result == committed


async def test_rollback_does_not_advance_graph_pointer(project, monkeypatch):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt)
    old = current(hub)
    event = hub._event

    def fail(c, kind, *args):
        if kind == "dags.updated":
            raise RuntimeError("Synthetic graph transaction failure")
        return event(c, kind, *args)

    monkeypatch.setattr(hub, "_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic"):
        await call(runner, attempt, "dag_update", args)
    assert current(hub) == old
    monkeypatch.setattr(hub, "_event", event)
    assert (await call(runner, attempt, "dag_update", args))["revision"] == 1


@pytest.mark.parametrize("kind,mode", [("ordinary", "ordinary"),
                                       ("ordinary", "message_only")])
async def test_unrelated_worker_reviewer_and_message_worker_can_save(project, kind, mode):
    hub, _, original, runner = project
    task = await hub.execute(Command(name="task.publish", key=uuid4().hex, payload={"contract": {
        "title": "Another task", "objective": "Maintain the shared planning graph."}}))
    worker = await hub.execute(Command(name="worker.register", key=uuid4().hex))
    attempt = await hub.execute(Command(name="task.claim", key=uuid4().hex, payload={
        "task_id": task["id"], "revision": 1, "worker_id": worker["id"], "run_id": original["run_id"]}))
    assert worker["kind"] == kind
    if mode == "message_only":
        await hub.db.call(lambda c: c.execute("UPDATE attempts SET execution_mode='message_only' WHERE id=?",
                                            (attempt["id"],)), write=True)
        attempt = {**attempt, "execution_mode": "message_only"}
    result = await call(runner, attempt, "dag_update", proposed(hub, attempt))
    assert result["revision"] == 1


async def test_decisions_have_no_tools_and_cannot_write_graphs(project):
    hub, _, attempt, _ = project
    run = await hub.run(attempt["run_id"])
    # The first executor's first turn prepares its task's recruitment decision; an idle
    # worker and a newly published task prepare an assignment decision.
    await hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    [recruitment] = await hub.due_recruitment_intents(run["id"])
    await idle_worker(hub, run, "Earlier task")
    await hub.execute(Command(name="task.publish", key=uuid4().hex, payload={"contract": {
        "title": "Next task", "objective": "Extend the shared planning graph."}}))
    assignment = await assignment_decision(hub, run["id"])
    graph = current(hub)
    assert graph["revision"] == 0
    for intent, answer, directive in ((recruitment, {"decision": False}, "recruitment_decided"),
                                      (assignment, {"worker": None}, "assigned")):
        harness = ScriptedHarness([ScriptedTurn([
            ScriptedAction("taskhub_call", {"operation": "resource_read",
                                            "arguments": {"root": "graphs", "path": "current.json"}}),
            ScriptedAction("taskhub_call", {"operation": "dag_update", "arguments": proposed(hub, attempt)}),
        ], visible_output=json.dumps(answer))])
        result = await WorkerRunner(hub, lambda harness=harness: harness).run_helper(intent)
        assert result["directive"] == directive
        # A decision has no tools: each attempted read or write is refused without effect.
        assert len(harness.results) == 2 and all(set(item) == {"error"} for item in harness.results)
        assert current(hub) == graph


async def test_message_worker_can_prepare_only_private_dag_files_through_fallback(project):
    hub, _, original, runner = project
    await hub.db.call(lambda c: c.execute("UPDATE attempts SET execution_mode='message_only' WHERE id=?",
                                        (original["id"],)), write=True)
    attempt = {**original, "execution_mode": "message_only"}
    for name in ("definitions", "proofs"):
        result = await call(runner, attempt, "workspace_write", {
            "path": f".private/dags/edit/{name}.json", "text": '{"nodes":[],"edges":[]}'}, name)
        assert "error" not in result
    for index, path in enumerate(("Other.lean", ".private/notes/x.md")):
        result = await call(runner, attempt, "workspace_write", {"path": path, "text": "denied"}, f"denied-{index}")
        assert "error" in result
    with pytest.raises(ValueError, match="declared root"):
        await call(runner, attempt, "workspace_write", {
            "path": ".private/dags/../notes/x.md", "text": "denied"}, "traversal")
    saved = await call(runner, attempt, "dag_update", {
        "source_dir": ".private/dags/edit", "expected_revision": 0, "summary": "Maintain planning after a directed message."})
    assert saved["revision"] == 1


async def test_all_scenarios_keep_graph_paths_and_exact_validator(project):
    hub, task, attempt, runner = project
    args = proposed(hub, attempt)
    packet, _ = await runner._native_packet({"task": task, "worker_id": attempt["worker_id"]}, attempt)
    native = packet["native"]
    assert native["shared_dags"] == current(hub)
    assert "shared-dags.md" in packet["instruction_guidance"]["optional_files"]
    checker = hub.workspace(attempt["worker_id"]) / native["directories"]["dag_checker"]
    from formal_geyser.coordination import dag_check
    assert checker.read_bytes() == Path(dag_check.__file__).read_bytes()
    process = subprocess.run([sys.executable, str(checker),
        str(hub.workspace(attempt["worker_id"]) / args["source_dir"] / "definitions.json"),
        str(hub.workspace(attempt["worker_id"]) / args["source_dir"] / "proofs.json")],
        capture_output=True, text=True, check=False)
    assert process.returncode == 0, process.stderr
    for mode in ("message_only", "ordinary"):
        entry = scenario_entry({**packet, "execution_mode": mode,
            "review": {"candidate_id": "fixture"} if mode == "ordinary" else None})
        assert entry["native"]["directories"]["graphs"] == str(hub.root / "graphs")
        assert entry["native"]["directories"]["dag_checker"]
        assert entry["native"]["shared_dags"] == current(hub)


async def test_reopen_recovers_missing_current_pointer(project):
    hub, _, attempt, runner = project
    reference = await call(runner, attempt, "dag_update", proposed(hub, attempt))
    Path(reference["current_path"]).unlink()
    await hub.close()
    reopened = Hub(hub.root)
    try:
        await reopened.db.call(lambda c: 1)
        assert current(reopened) == reference
    finally:
        await reopened.close()


async def test_graph_resource_cannot_be_shadowed_and_fence_is_enforced(project):
    hub, _, attempt, runner = project
    args = proposed(hub, attempt)
    actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                  fencing_token=attempt["fencing_token"] + 1)
    with pytest.raises(HubError) as error:
        await hub.execute(Command(name="dag.update", key=uuid4().hex, payload=args), actor)
    assert error.value.code == "STALE_BINDING"
    hub.config.environment["resource_roots"] = {"graphs": str(hub.root / "other")}
    with pytest.raises(ValueError, match="reserved"):
        WorkerRunner(hub, ScriptedHarness)


def test_checker_is_copied_without_a_package_import_requirement(tmp_path):
    relative = prepare_dag_checker(tmp_path)
    result = subprocess.run([sys.executable, str(tmp_path / relative), "--help"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "definitions" in result.stdout.lower()


async def test_real_reviewer_can_maintain_public_planning_graphs(tmp_path):
    from tests.coordination.lifecycle_helpers import select_review
    from tests.coordination.publication_helpers import file_payload, new_hub

    hub = await new_hub(tmp_path)
    try:
        async def command(name, payload=None, actor=None):
            return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)
        await command("task.publish", {"contract": {"title": "Graph review fixture", "objective": "Review the candidate."}})
        run = await command("run.start")
        author = await hub.next_assignment(run["id"])
        actor = Actor(role=Role.WORKER, worker_id=author["worker_id"], attempt_id=author["id"],
                      fencing_token=author["fencing_token"])
        staged = await command("artifact.stage", file_payload(hub, author, data={"ready": True}), actor)
        reviewer = await select_review(hub, staged["review_task_id"], run["id"])
        result = await call(WorkerRunner(hub, ScriptedHarness), reviewer, "dag_update", proposed(hub, reviewer))
        assert result["revision"] == 1
        identity = await hub.worker(reviewer["worker_id"])
        assert identity["display_name"] == "Reviewer 1" and identity["worker_number"] is None
    finally:
        await hub.close()
