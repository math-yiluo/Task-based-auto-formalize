"""Native catalog evidence is complete, thread-bound, and checked before inference."""
from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
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
from formal_geyser.coordination.runtime.actions import ActionCatalog
from formal_geyser.coordination.runtime.catalog import (
    CatalogError,
    catalog_binding,
    normalize_catalog,
)
from formal_geyser.coordination.runtime.codex import CodexHarness
from formal_geyser.coordination.runtime.rpc import RpcClosed, RpcTimeout
from formal_geyser.coordination.runtime.tools import operation_tools
from tests.coordination.lifecycle_helpers import assignment_decision, record_saved_turn

TOOL = {"type": "function", "name": "check", "description": "Check the saved file.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"], "additionalProperties": False}}


async def command(hub, name, payload=None, *, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex),
                             Actor(role=role))


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    await command(hub, "task.publish", {"contract": {
        "title": "Retain the original conversation", "objective": "Inspect native definitions."}})
    run = await command(hub, "run.start")
    try:
        yield hub, run
    finally:
        await hub.close()


async def bind(hub, worker_id, catalog, *, thread_id="saved-thread"):
    await command(hub, "worker.bind_session", {
        "worker_id": worker_id, "thread_id": thread_id, "model": "scripted",
        "config_hash": "retained", **catalog}, role=Role.RUNNER)
    await record_saved_turn(hub, worker_id)


def persisted(catalog):
    result = deepcopy(catalog)
    for tool in result:
        tool.pop("type", None)
        tool["input_schema"] = tool.pop("inputSchema")
        tool["namespace"] = None
        tool["defer_loading"] = False
    return list(reversed(result))


def test_serialization_aliases_defaults_and_catalog_order_preserve_exact_digest():
    tools = [deepcopy(TOOL), {**deepcopy(TOOL), "name": "second"}]
    assert catalog_binding(persisted(tools)) == catalog_binding(tools)
    assert normalize_catalog(persisted(tools)) == normalize_catalog(tools)


@pytest.mark.parametrize("change", [
    {"description": None}, {"inputSchema": None}, {"inputSchema": []},
    {"inputSchema": {"required": "path"}}, {"name": ""}, {"name": " padded "},
    {"type": "custom"}, {"namespace": 12}, {"defer_loading": None},
    {"unknown_future_field": True}, {"input_schema": {"type": "string"}},
    {"inputSchema": {"minimum": float("nan")}},
])
def test_malformed_or_ambiguous_definitions_fail_closed(change):
    with pytest.raises(CatalogError):
        normalize_catalog([{**deepcopy(TOOL), **change}])


def test_duplicate_names_and_incomplete_definitions_are_not_filtered_out():
    for catalog in ([TOOL, deepcopy(TOOL)], [TOOL, None], [{"name": "check"}], {}):
        with pytest.raises(CatalogError):
            normalize_catalog(catalog)


def native_reader(tmp_path, monkeypatch, *, thread_id="saved-thread", payload=None, text=None,
                  outside=False):
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    path = (tmp_path if outside else home) / "record.jsonl"
    if text is None:
        text = json.dumps({"type": "session_meta", "payload": payload or {
            "id": thread_id, "dynamic_tools": persisted([TOOL])}}) + "\n"
    path.write_text(text, encoding="utf-8")
    harness = CodexHarness(CodexConfig(home=str(home)), command=["unused"])
    reads = []

    async def read_history(requested, *, workspace, include_turns):
        reads.append((requested, workspace, include_turns))
        return {"thread": {"id": requested, "path": str(path)}}

    monkeypatch.setattr(harness, "read_history", read_history)
    return harness, path, reads


async def test_reads_only_authoritative_metadata_and_retains_full_definitions(tmp_path, monkeypatch):
    metadata = {"type": "session_meta", "payload": {
        "id": "saved-thread", "dynamic_tools": persisted([TOOL])}}
    text = json.dumps(metadata) + "\nthis turn content must never be read\n"
    harness, path, reads = native_reader(tmp_path, monkeypatch, text=text)
    assert await harness.stored_tool_catalog("saved-thread", workspace=tmp_path) == [TOOL]
    assert reads == [("saved-thread", tmp_path, False)]
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("payload", [
    {"id": "another-thread", "dynamic_tools": [TOOL]},
    {"dynamic_tools": [TOOL]}, {"id": "saved-thread", "dynamic_tools": {}},
    {"id": "saved-thread", "dynamic_tools": [TOOL, TOOL]},
    {"id": "saved-thread", "dynamic_tools": [{"name": "check"}]},
    "not-an-object",
])
async def test_native_metadata_requires_matching_identity_and_valid_catalog(tmp_path, monkeypatch, payload):
    harness, _, _ = native_reader(tmp_path, monkeypatch, payload=payload)
    with pytest.raises(CatalogError):
        await harness.stored_tool_catalog("saved-thread", workspace=tmp_path)


@pytest.mark.parametrize("text", [
    "not json\n", "[]\n", '{"type":"session_meta","type":"other","payload":{}}\n',
    "\n", '{"type":"event_msg"}\n' * 21,
])
async def test_malformed_or_missing_metadata_is_not_bypassed_by_history_search(tmp_path, monkeypatch, text):
    harness, _, _ = native_reader(tmp_path, monkeypatch, text=text)
    # Another file with valid metadata is irrelevant to thread/read's path.
    (tmp_path / "codex-home" / "unrelated.jsonl").write_text(json.dumps({
        "type": "session_meta", "payload": {"id": "saved-thread", "dynamic_tools": [TOOL]}}))
    with pytest.raises(CatalogError):
        await harness.stored_tool_catalog("saved-thread", workspace=tmp_path)


async def test_session_path_outside_home_is_not_accepted(tmp_path, monkeypatch):
    harness, _, _ = native_reader(tmp_path, monkeypatch, outside=True)
    with pytest.raises(CatalogError, match="outside"):
        await harness.stored_tool_catalog("saved-thread", workspace=tmp_path)


@pytest.mark.parametrize("payload", [{"id": "saved-thread"}, {"id": "saved-thread", "dynamic_tools": None}])
async def test_absent_native_catalog_means_empty_not_current(tmp_path, monkeypatch, payload):
    harness, _, _ = native_reader(tmp_path, monkeypatch, payload=payload)
    assert await harness.stored_tool_catalog("saved-thread", workspace=tmp_path) == []


@pytest.mark.parametrize("difference", [
    "description", "schema", "obsolete", "malformed", "duplicate", "unavailable", "direct_actions",
])
@pytest.mark.parametrize("cached_hash", ["current", "older"])
async def test_same_name_cache_never_admits_incompatible_native_catalog(project, difference, cached_hash):
    hub, run = project
    reads, opened, prompts = [], [], []
    saved = []

    class NativeCatalog(ScriptedHarness):
        async def stored_tool_catalog(self, thread_id, *, workspace):
            reads.append((thread_id, workspace))
            if difference == "unavailable":
                raise OSError("Native session file is unavailable")
            return deepcopy(saved)

        async def open(self, **kwargs):
            opened.append(kwargs)
            return await super().open(**kwargs)

        async def run_turn(self, prompt, **kwargs):
            prompts.append(prompt)
            return await super().run_turn(prompt, **kwargs)

    runner = WorkerRunner(hub, NativeCatalog)
    saved.extend(deepcopy(runner.tools))
    if difference == "description":
        saved[0]["description"] = "Old instructions for a different contract."
    elif difference == "schema":
        saved[0]["inputSchema"] = {"type": "object", "properties": {"old_payload": {"type": "string"}}}
    elif difference == "obsolete":
        saved.append({**deepcopy(TOOL), "name": "obsolete_tool"})
    elif difference == "malformed":
        saved[0].pop("description")
    elif difference == "duplicate":
        saved.append(deepcopy(saved[0]))
    elif difference == "direct_actions":
        saved[:] = operation_tools()
    attempt = await hub.next_assignment(run["id"])
    worker_id = attempt["worker_id"]
    cache = runner._catalog_binding()
    if cached_hash == "older":
        cache["tool_catalog_hash"] = "0" * 64
    # Even a prior verification marker must never bypass a fresh native read.
    cache["tool_catalog_verified_thread_id"] = "saved-thread"
    await bind(hub, worker_id, cache)
    before = await hub.worker(worker_id)
    preserved = hub.workspace(worker_id) / "saved.md"
    preserved.write_text("Exact saved work λ\n", encoding="utf-8")

    with pytest.raises(HubError) as failure:
        await runner.run_once(run["id"], assignment=attempt)
    assert failure.value.code == "NATIVE_CATALOG_MIGRATION_REQUIRED"
    assert reads == [("saved-thread", hub.workspace(worker_id))]
    assert opened == prompts == []
    after = await hub.worker(worker_id)
    assert after["thread_id"] == before["thread_id"]
    assert after["profile_json"] == before["profile_json"]
    assert preserved.read_text(encoding="utf-8") == "Exact saved work λ\n"
    state = await hub.run(run["id"])
    assert state["state"] == "running" and state["turns_used"] == 0 and state["tokens_used"] == 0
    assert not state["usage_uncertain"]


@pytest.mark.parametrize("failure", [RpcTimeout("thread/read"), RpcClosed("Codex exited"), ConnectionError("reset")])
async def test_a_probe_that_cannot_reach_codex_is_retried_not_held(project, failure):
    hub, run = project
    opened = []

    class Unreachable(ScriptedHarness):
        async def stored_tool_catalog(self, thread_id, *, workspace):
            raise failure

        async def open(self, **kwargs):
            opened.append(kwargs)
            return await super().open(**kwargs)

    runner = WorkerRunner(hub, Unreachable)
    attempt = await hub.next_assignment(run["id"])
    await bind(hub, attempt["worker_id"], runner._catalog_binding())
    with pytest.raises(type(failure)):
        await runner.run_once(run["id"], assignment=attempt)
    assert opened == []  # Unverified, the saved conversation is never resumed.
    events = {event["kind"] for event in await hub.events(limit=1000)}
    assert "runtime.catalog_held" not in events
    assert (await hub.worker(attempt["worker_id"]))["thread_id"] == "saved-thread"


async def test_operation_schema_evolution_does_not_change_or_replace_native_catalog(project):
    hub, _ = project
    reads, saved = [], []

    class NativeCatalog(ScriptedHarness):
        async def stored_tool_catalog(self, thread_id, *, workspace):
            reads.append(thread_id)
            return deepcopy(saved)

    runner = WorkerRunner(hub, NativeCatalog)
    saved.extend(deepcopy(runner.tools))
    before = runner._catalog_binding()
    worker = await command(hub, "worker.register")
    await bind(hub, worker["id"], before)
    evolved = runner.actions.specs
    publish = next(item for item in evolved if item["name"] == "taskhub_publish")
    publish["inputSchema"]["properties"]["future_option"] = {"type": "boolean"}
    evolved.append({"type": "function", "name": "lean_new_read", "description": "Read domain data.",
                    "inputSchema": {"type": "object", "additionalProperties": False}})
    runner.actions = ActionCatalog(evolved)
    assert "future_option" in runner.actions.describe(operation="publish")["inputSchema"]["properties"]
    assert runner._catalog_binding() == before
    assert await runner._ensure_catalog(await hub.worker(worker["id"]), harness=NativeCatalog()) == {
        **before, "tool_catalog_verified_thread_id": "saved-thread"}
    assert reads == ["saved-thread"]


@pytest.mark.parametrize("profile_json", [None, "null", "malformed-json", "[]"])
async def test_successful_native_evidence_uses_actual_definitions_and_target_thread(project, profile_json):
    hub, _ = project
    saved, reads = [], []

    class NativeCatalog(ScriptedHarness):
        async def stored_tool_catalog(self, thread_id, *, workspace):
            reads.append(thread_id)
            return persisted(saved)

    runner = WorkerRunner(hub, NativeCatalog)
    saved.extend(deepcopy(runner.tools))
    worker = await command(hub, "worker.register")
    await bind(hub, worker["id"], {"tool_catalog_hash": "obsolete-cache"})
    worker = await hub.worker(worker["id"])
    if profile_json is not None:
        worker["profile_json"] = profile_json
    binding = await runner._ensure_catalog(worker, harness=NativeCatalog())
    assert binding == {**runner._catalog_binding(), "tool_catalog_verified_thread_id": "saved-thread"}
    assert reads == ["saved-thread"]
    transcript = runner._catalog_transcript(binding, thread_id="saved-thread")
    assert transcript["tool_catalog"]["mode"] == "persisted"
    assert transcript["tool_catalog"]["schemas"] == "verified"
    assert "tools" not in transcript


async def test_plain_scripted_harness_needs_no_native_catalog(project):
    hub, run = project
    harness = ScriptedHarness([ScriptedTurn([])])
    runner = WorkerRunner(hub, lambda: harness)
    attempt = await hub.next_assignment(run["id"])
    await bind(hub, attempt["worker_id"], {})
    await runner.run_once(run["id"], assignment=attempt)
    assert harness.opened_threads == ["saved-thread"]
    assert len(harness.prompts) == 1
    profile = json.loads((await hub.worker(attempt["worker_id"]))["profile_json"])
    assert "tool_catalog_hash" not in profile


async def test_decision_needs_no_catalog_evidence_from_a_legacy_idle_worker(project):
    hub, run = project
    opened, reads = [], []

    class LegacyCatalog(ScriptedHarness):
        async def stored_tool_catalog(self, thread_id, *, workspace):
            reads.append(thread_id)
            return [TOOL]

        async def open(self, **kwargs):
            opened.append(kwargs)
            return await super().open(**kwargs)

    worker = await command(hub, "worker.register")
    await bind(hub, worker["id"], {})  # An idle worker whose saved conversation has a legacy catalog.
    intent = await assignment_decision(hub, run["id"])
    answer = json.dumps({"worker": (await hub.worker(worker["id"]))["display_name"]})
    runner = WorkerRunner(hub, lambda: LegacyCatalog([ScriptedTurn([], visible_output=answer)]))
    result = await runner.run_helper(intent)
    # The decision starts a new conversation without tools, so no catalog is read or can hold it.
    assert result["directive"] == "assigned" and result["outcome"] == "idle_worker"
    assert reads == []
    [options] = opened
    assert options["tools"] == [] and options["thread_id"] is None
    state = await hub.run(run["id"])
    assert state["state"] == "running" and state["turns_used"] == 1 and not state["usage_uncertain"]
    saved = await hub.db.call(lambda c: dict(c.execute(
        "SELECT * FROM helper_intents WHERE id=?", (intent["id"],)).fetchone()))
    assert saved["state"] == "completed"
    # The chosen worker starts its new task in a new conversation: the legacy one is not resumed.
    assert (await hub.worker(worker["id"]))["thread_id"] is None
