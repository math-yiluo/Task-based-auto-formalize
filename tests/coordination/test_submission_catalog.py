"""Installed gateway catalogs and historical metadata remain truthful without inference."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import (
    ScriptedAction,
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from tests.coordination.lifecycle_helpers import assignment_decision, record_saved_turn
from tests.coordination.test_native_session_delivery import native


async def command(hub, name, payload=None, *, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex),
                             Actor(role=role))


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    task = await command(hub, "task.publish", {"contract": {
        "title": "Preserve submission compatibility", "objective": "Inspect saved records."}})
    run = await command(hub, "run.start")
    try:
        yield hub, task, run
    finally:
        await hub.close()


def old_catalog(runner):
    return {"tool_catalog_version": 2,
            "tool_catalog_names": sorted({tool["name"] for tool in runner.tools} | {"lean_stage_record"}),
            "tool_catalog_hash": "a" * 64}


async def bind(hub, worker_id, catalog):
    await command(hub, "worker.bind_session", {
        "worker_id": worker_id, "thread_id": "historical-source", "model": "scripted",
        "config_hash": "historical-config", "codex_home": hub.config.codex.home, **catalog},
        role=Role.RUNNER)
    await record_saved_turn(hub, worker_id)


def instructions(runner, worker_id, session_key):
    page = runner.transcripts.session_page(worker_id, session_key)
    return [item["instructions"] for item in page["context"] if item["type"] == "instructions"][-1]


async def idle_with_catalog(hub, run, catalog):
    """An idle worker whose saved conversation recorded `catalog`: it executed a task and left it."""
    earlier = await command(hub, "task.publish", {"contract": {
        "title": "Earlier saved work", "objective": "Inspect saved records."}})
    worker_id = (await command(hub, "worker.register"))["id"]
    await bind(hub, worker_id, catalog)
    attempt = await command(hub, "task.claim", {"task_id": earlier["id"], "revision": earlier["revision"],
                                               "worker_id": worker_id, "run_id": run["id"]})
    await hub.ordinary_turn_completed(worker_id, attempt["id"])
    await command(hub, "task.leave", {"task_id": earlier["id"], "worker_id": worker_id, "permanent": True,
                                      "reason": "The earlier executor finished its part and leaves."})
    return worker_id


@pytest.mark.parametrize("historical", [False, True])
async def test_start_installs_gateway_and_scripted_resume_preserves_recorded_metadata(project, historical):
    hub, _, run = project
    harness = ScriptedHarness([ScriptedTurn([])])
    runner = WorkerRunner(hub, lambda: harness)
    assert {tool["name"] for tool in runner.tools} == {"taskhub_actions", "taskhub_call"}
    attempt = await hub.next_assignment(run["id"])
    expected = old_catalog(runner) if historical else runner._catalog_binding()
    if historical:
        await bind(hub, attempt["worker_id"], expected)
    await runner.run_once(run["id"], assignment=attempt)

    worker = await hub.worker(attempt["worker_id"])
    profile = json.loads(worker["profile_json"])
    assert {key: profile[key] for key in expected} == expected
    saved = instructions(runner, worker["id"], "thread:" + worker["thread_id"])
    if historical:
        assert harness.opened_threads == ["historical-source"]
        assert "tools" not in saved
        assert saved["tool_catalog"] == {
            "mode": "persisted", "source_thread_id": "historical-source",
            "schemas": "not_loaded", **expected}
        assert profile["tool_catalog_hash"] != runner._catalog_binding()["tool_catalog_hash"]
    else:
        assert saved["tools"] == runner.tools
        assert saved["tool_catalog"] == {"mode": "installed", **expected}


@pytest.mark.parametrize("kind", ["assignment", "recruitment"])
async def test_decisions_inherit_no_catalog_and_answer_legacy_mutation_without_effect(project, kind):
    hub, _, run = project
    opened = []

    class Decision(ScriptedHarness):
        async def open(self, **options):
            opened.append(options)
            return await super().open(**options)

    initial = ScriptedHarness([ScriptedTurn([])])
    decision = {"worker": None} if kind == "assignment" else {"decision": False}
    # The decision model tries a legacy mutation tool and a gateway mutation anyway.
    attempts = [ScriptedAction("lean_stage_record", {}), ScriptedAction("taskhub_call", {
        "operation": "stage", "arguments": {"manifest_path": "candidate.json"}})]
    helper = Decision([ScriptedTurn(attempts, visible_output=json.dumps(decision))])
    harnesses = iter([initial, helper] if kind == "recruitment" else [helper])
    runner = WorkerRunner(hub, lambda: next(harnesses))
    expected = old_catalog(runner)
    if kind == "recruitment":
        attempt = await hub.next_assignment(run["id"])
        worker_id = attempt["worker_id"]
        await bind(hub, worker_id, expected)
        await runner.run_once(run["id"], assignment=attempt)
        intent = (await hub.due_recruitment_intents(run["id"]))[0]
    else:
        # The offered idle worker's saved conversation has the historical catalog.
        worker_id = await idle_with_catalog(hub, run, expected)
        intent = await assignment_decision(hub, run["id"])
        assert [item["worker_id"] for item in intent["candidates"]["workers"]] == [worker_id]
    artifacts = len(await hub.list_artifacts())
    result = await runner.run_helper(intent)

    assert result["directive"] == ("assigned" if kind == "assignment" else "recruitment_decided")
    # A new conversation without tools: no worker history or catalog is inherited.
    [options] = opened
    assert options["tools"] == [] and options["thread_id"] is None
    # Every tool call is answered with an error and changes nothing.
    assert len(helper.results) == 2 and all("error" in item for item in helper.results)
    assert len(await hub.list_artifacts()) == artifacts
    worker = await hub.worker(worker_id)
    assert worker["thread_id"] == "historical-source"
    assert json.loads(worker["profile_json"])["tool_catalog_hash"] == expected["tool_catalog_hash"]
    # The decision's transcript records the tools it actually had: none, with no inherited catalog.
    saved = instructions(runner, intent["source_worker_id"], "helper:" + intent["id"])
    catalog = saved.get("tool_catalog", {})
    assert catalog.get("mode") != "inherited" and "source_thread_id" not in catalog
    assert saved.get("tools", []) == [] and not catalog.get("tool_catalog_names")


async def test_unknown_schema_is_not_fabricated_from_current_catalog(project):
    hub, _, run = project
    runner = WorkerRunner(hub, lambda: ScriptedHarness([ScriptedTurn([])]))
    attempt = await hub.next_assignment(run["id"])
    await bind(hub, attempt["worker_id"], {})
    await runner.run_once(run["id"], assignment=attempt)

    worker = await hub.worker(attempt["worker_id"])
    profile = json.loads(worker["profile_json"])
    saved = instructions(runner, worker["id"], "thread:" + worker["thread_id"])
    assert "tools" not in saved
    assert saved["tool_catalog"]["mode"] == "persisted"
    assert saved["tool_catalog"]["schemas"] == "not_loaded"
    for record in (profile, saved["tool_catalog"]):
        assert "tool_catalog_hash" not in record
        assert "tool_catalog_version" not in record
        assert "tool_catalog_names" not in record


@pytest.mark.parametrize("mode", ["start", "resume"])
async def test_native_config_hash_does_not_claim_to_replace_persisted_tools(tmp_path, mode):
    hashes = []
    for tools in ([], [{"type": "function", "name": "different_catalog", "description": "Test",
                       "inputSchema": {"type": "object"}}]):
        harness, _ = native(tmp_path)
        try:
            session = await harness.open(workspace=tmp_path,
                thread_id="source" if mode == "resume" else None,
                tools=tools, instructions="Unchanged instructions.")
            hashes.append(session.config_hash)
        finally:
            await harness.close()
    assert (hashes[0] != hashes[1]) is (mode == "start")
