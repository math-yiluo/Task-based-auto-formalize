"""Real readiness/API gates with local cache evidence and no model or Lean calls."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from formal_geyser.bootstrap_pins import LEAN_VERSION, pinned_versions
from formal_geyser.bootstrap_runtime import NOT_READY
from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import WorkerRunner
from formal_geyser.coordination.runtime.entries import scenario_entry
from formal_geyser.coordination.storage import atomic_json
from formal_geyser.registry import ObjectRegistry, file_sha256, library_manifest
from formal_geyser.schemas import EnvironmentRecord, LibraryRoot
from formal_geyser.worker_tools import handle_worker_tool
from tests.coordination.lifecycle_helpers import assignment_decision, start_helper
from tests.coordination.publication_helpers import new_hub
from tests.test_bootstrap_cache import fake_prepared_cache

OPERATOR = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


async def register(hub, record):
    value = await ObjectRegistry(hub).stage(record, OPERATOR)
    await command(hub, "artifact.accept", {"artifact_id": value["id"], "policy": "lean.environment@1",
        "verdict": "accepted", "details": {"scope": "Synthetic initialization fixture, no proof claimed"}})
    return value["id"]


@pytest.fixture
async def ready(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("br")
    cache, fake = await fake_prepared_cache(root, monkeypatch)
    # External preparation commands are synthetic. The runtime validator and
    # every Hub/API admission check below run unmodified against real files.
    hub = await new_hub(root / "project", config=ProjectConfig(domain="lean"))
    record = EnvironmentRecord(name="Synthetic pinned environment",
        lean_executable=cache["lean_executable"], lean_executable_sha256=file_sha256(Path(cache["lean_executable"])),
        lean_version=f"Lean (version {LEAN_VERSION}, synthetic fixture)", local_execution="trusted-code-only",
        library_roots=[LibraryRoot(path=path, manifest_sha256=hub.blobs.put_json(library_manifest(Path(path))))
                       for path in cache["library_roots"]])
    environment_id = await register(hub, record)
    state = {"format": 1, "state": "ready", "pins": pinned_versions(), "cache": cache,
        "environment_artifact": environment_id, "initial_snapshot": hub.blobs.put_json({"files": []}),
        "initial_commit": hub._repository().head(), "seed_check": {"ok": True, "checked_modules": []}}
    path = hub.state_dir / "initialization.json"
    atomic_json(path, state)
    try:
        yield SimpleNamespace(hub=hub, state=state, path=path, cache=cache, fake=fake, environment=record)
    finally:
        await hub.close()


async def task_and_run(case):
    task = await command(case.hub, "task.publish", {"contract": {"title": "Task 0", "objective": "Inspect fixture source",
        "domain": "lean", "acceptance_policy": "lean.peer@1"}})
    run = await command(case.hub, "run.start")
    return task, run


@pytest.mark.parametrize("failure", ["missing", "preparing", "failed", "pins", "cache_receipt", "olean", "compiler", "approval", "environment_roots"])
async def test_start_requires_current_ready_cache_and_matching_approval(ready, failure):
    case = ready
    if failure == "missing":
        case.path.unlink()
    elif failure in {"preparing", "failed"}:
        atomic_json(case.path, {**case.state, "state": failure})
    elif failure == "pins":
        atomic_json(case.path, {**case.state, "pins": {**pinned_versions(), "mathlib_commit": "0" * 40}})
    elif failure == "cache_receipt":
        Path(case.cache["receipt_path"]).unlink()
    elif failure == "olean":
        Path(case.cache["sentinels"][0]["path"]).unlink()
    elif failure == "compiler":
        Path(case.cache["lean_executable"]).write_bytes(b"a replaced compiler")
    elif failure == "approval":
        await command(case.hub, "artifact.withdraw", {"artifact_id": case.state["environment_artifact"],
            "reason": "The synthetic prepared environment is no longer approved."})
    else:
        other = await register(case.hub, case.environment.model_copy(update={"name": "Wrong roots", "library_roots": []}))
        atomic_json(case.path, {**case.state, "environment_artifact": other})
    before = len(case.fake.calls)
    with pytest.raises(HubError) as rejected:
        await command(case.hub, "run.start")
    assert rejected.value.code == NOT_READY
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM runs").fetchone()[0]) == 0
    assert await case.hub.list_workers() == []
    assert len(case.fake.calls) == before


async def test_cache_cannot_be_borrowed_from_another_project_instance(ready, tmp_path):
    hub = await new_hub(tmp_path / "other", config=ProjectConfig(domain="lean"))
    try:
        atomic_json(hub.state_dir / "initialization.json", ready.state)
        with pytest.raises(HubError, match="this project's .dependencies"):
            await command(hub, "run.start")
    finally:
        await hub.close()


async def test_api_start_and_resume_fail_without_rearming_execution(ready):
    hub = ready.hub
    _, run = await task_and_run(ready)
    await command(hub, "run.pause", {"run_id": run["id"]})
    atomic_json(ready.path, {**ready.state, "state": "preparing"})
    def forbidden(_hub):
        raise AssertionError("Unready API request created a native runner")
    app = create_app(hub.root, hub=hub, runner_factory=forbidden)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            for name, payload in [("run.start", {}), ("run.resume", {"run_id": run["id"]})]:
                result = await client.post("/v1/commands",
                    json=Command(name=name, key=uuid4().hex, payload=payload).model_dump(mode="json"))
                assert result.status_code == 409 and result.json()["error"]["code"] == NOT_READY
        assert not app.state.driver.armed and not app.state.driver.passes
    assert (await hub.run(run["id"]))["state"] == "paused"


async def test_assignments_turns_and_decision_admission_recheck_readiness(ready):
    hub = ready.hub
    _, run = await task_and_run(ready)
    attempt = await hub.next_assignment(run["id"])
    helper = await hub.record_first_turn(attempt["worker_id"], attempt["task_id"], "synthetic-turn")
    assert helper is not None
    unassigned = await command(hub, "worker.register")
    # The idle worker is offered the next task in one assignment decision.
    await command(hub, "task.publish", {"contract": {"title": "Next fixture task", "objective": "Inspect more fixture source",
        "domain": "lean", "acceptance_policy": "lean.peer@1"}})
    decision = await assignment_decision(hub, run["id"])
    assert [item["worker_id"] for item in decision["candidates"]["workers"]] == [unassigned["id"]]
    workers = await hub.list_workers()
    atomic_json(ready.path, {**ready.state, "state": "failed"})
    for operation in (
        lambda: hub.next_assignment(run["id"]),
        lambda: command(hub, "turn.reserve", {"worker_id": attempt["worker_id"]}, RUNNER),
        lambda: start_helper(hub, helper["id"], "must-not-start"),
        lambda: start_helper(hub, decision["id"], "must-not-start"),
        lambda: hub.complete_assignment(decision["id"], unassigned["display_name"]),
        lambda: command(hub, "task.claim", {"worker_id": unassigned["id"], "task_id": attempt["task_id"],
            "revision": 1, "run_id": run["id"]}),
    ):
        with pytest.raises(HubError) as rejected:
            await operation()
        assert rejected.value.code == NOT_READY
    assert await hub.due_assignment_intents(run["id"]) == []
    assert not await hub.db.call(lambda c: c.execute("SELECT 1 FROM turn_intents").fetchone())
    for intent in (helper, decision):
        assert (await hub.db.call(lambda c: hub._helper(c, intent["id"])))["state"] == "prepared"
    assert await hub.list_workers() == workers  # Nobody was created or assigned.
    # Safety control and quiet cleanup remain available with a missing cache.
    await command(hub, "run.pause", {"run_id": run["id"]})
    await command(hub, "runtime.abandon", {"worker_id": attempt["worker_id"], "quiescent": True,
        "reason": "Synthetic preparation failure; no native process was started."}, RUNNER)
    assert (await hub.worker(attempt["worker_id"]))["active_attempt"] is None


async def test_native_launch_rechecks_after_assignment_without_opening_a_process(ready):
    hub = ready.hub
    _, run = await task_and_run(ready)
    attempt = await hub.next_assignment(run["id"])
    called = []
    def forbidden():
        called.append(True)
        raise AssertionError("Unready worker reached the native factory")
    runner = WorkerRunner(hub, forbidden)
    atomic_json(ready.path, {**ready.state, "state": "preparing"})
    with pytest.raises(HubError) as rejected:
        await runner.run_once(run["id"], assignment=attempt)
    assert rejected.value.code == NOT_READY and not called
    assert (await hub.worker(attempt["worker_id"]))["active_attempt"] is None


async def test_task_binding_context_and_native_factory_use_the_same_shared_environment(ready, monkeypatch):
    import os

    async def probe(*args):
        return {"ok": True}  # Native restricted-token behavior is tested separately.
    monkeypatch.setattr("formal_geyser.coordination.native_permissions.ensure_native_access", probe)
    hub = ready.hub
    before = len(ready.fake.calls)
    task, run = await task_and_run(ready)
    assert task["contract"]["payload"]["environment_artifact"] == ready.state["environment_artifact"]
    assert ready.state["environment_artifact"] in task["contract"]["inputs"]
    attempt = await hub.next_assignment(run["id"])
    native = await hub.prepare_native_context(attempt["worker_id"])
    assert native["lean_environment"]["environment_artifact"] == ready.state["environment_artifact"]
    assert native["read_only_resources"]["shared_mathlib"] == ready.cache["cache_directory"]
    # Workers read the shared Mathlib sources and compile with lean_check, in the Lean service:
    # the context names no Lean or Lake executable and no compiled library.
    assert native["lean_environment"]["source_roots"] == ready.cache["source_roots"]
    assert not {"lean_executable", "lake_executable", "library_roots"}.intersection(native["lean_environment"])
    assert "lean_check" in native["lean_environment"]["instruction"]
    view = scenario_entry({"task": {"id": task["id"]}, "execution_mode": "message", "native": native})
    assert view["native"]["lean_environment"] == native["lean_environment"]
    runner = WorkerRunner(hub)
    monkeypatch.setattr("formal_geyser.coordination.runtime.codex.resolve_codex_executable",
                        lambda executable: "/usr/bin/codex")
    harness = await runner._new_harness()  # Constructor only; never open native transport.
    environment = harness.process_environment()
    # The native shell sees the same sources, and no Lean or Lake.
    assert all(str(Path(path).resolve()) in environment["LEAN_SRC_PATH"].split(os.pathsep)
               for path in ready.cache["source_roots"])
    assert not {"LEAN", "LAKE", "LEAN_PATH", "LEAN_SYSROOT", "LAKE_HOME"}.intersection(environment)
    assert str(Path(ready.cache["lean_executable"]).parent) not in environment["PATH"].split(os.pathsep)
    assert not (hub.workspace(attempt["worker_id"]) / ".lake").exists()
    assert len(ready.fake.calls) == before


async def test_explicit_other_environment_is_rejected_for_new_tasks_and_checks(ready):
    hub = ready.hub
    other = await register(hub, ready.environment.model_copy(update={"name": "Another approved identity"}))
    task, run = await task_and_run(ready)
    with pytest.raises(HubError) as rejected:
        await command(hub, "task.publish", {"contract": {**task["contract"], "title": "Wrong environment",
            "payload": {"environment_artifact": other}}})
    assert rejected.value.code == "LEAN_ENVIRONMENT_MISMATCH"
    attempt = await hub.next_assignment(run["id"])
    actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
        fencing_token=attempt["fencing_token"])
    from formal_geyser.registry import RegistryError

    with pytest.raises(RegistryError, match="prepared Lean environment"):
        await handle_worker_tool(hub, "lean_check", {"environment_artifact": other, "modules": ["Fixture"]}, actor, key="wrong")


async def test_missing_marker_does_not_prevent_operator_record_maintenance(ready):
    ready.path.write_text("not valid JSON", encoding="utf-8")
    task = await command(ready.hub, "task.publish", {"contract": {"title": "Repair retained source", "objective": "Preserve records",
        "domain": "lean", "acceptance_policy": "lean.peer@1"}})
    assert task["state"] == "ready"
    await command(ready.hub, "artifact.withdraw", {"artifact_id": ready.state["environment_artifact"], "reason": "Retain invalidation evidence"})
    assert (await ready.hub.artifact(ready.state["environment_artifact"]))["status"] == "withdrawn"


async def test_prepared_files_are_rechecked_when_markers_change_and_each_minute(ready, monkeypatch):
    import os
    import time

    from formal_geyser import bootstrap_cache, bootstrap_runtime

    case = ready
    old = time.time() - 3600
    for marker in (case.cache["receipt_path"], str(Path(case.cache["cache_directory"]) / "owner.json"),
                   case.cache["lean_executable"], case.cache["lake_executable"],
                   *(item["path"] for item in case.cache["sentinels"])):
        os.utime(marker, (old, old))
    calls = []
    original = bootstrap_cache.validate_cache
    monkeypatch.setattr(bootstrap_cache, "validate_cache", lambda receipt: calls.append(1) or original(receipt))
    _, run = await task_and_run(case)
    await case.hub.next_assignment(run["id"])
    assert len(calls) == 1  # Prepared files were checked once; the ledger was checked each time.
    monkeypatch.setattr(bootstrap_runtime, "PREPARED_FILES_SECONDS", 0.0)
    await case.hub.next_assignment(run["id"])
    assert len(calls) == 2  # The full check repeats at least once a minute.
    monkeypatch.setattr(bootstrap_runtime, "PREPARED_FILES_SECONDS", 60.0)
    Path(case.cache["sentinels"][0]["path"]).unlink()  # A presence marker disappears.
    with pytest.raises(HubError) as rejected:
        await case.hub.next_assignment(run["id"])
    assert rejected.value.code == NOT_READY
