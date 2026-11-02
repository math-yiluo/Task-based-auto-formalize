"""Public wiki submission preserves real authorship and replacement gates."""
from __future__ import annotations

import re
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    RepositoryConfig,
    Role,
)
from formal_geyser.coordination.runtime import ScriptedHarness, WorkerRunner
from formal_geyser.coordination.runtime.helpers import DECISION_INSTRUCTIONS
from formal_geyser.coordination.runtime.tools import (
    TURN_END_INSTRUCTIONS,
    WORKER_INSTRUCTIONS,
    dynamic_tools,
    operation_tools,
)
from tests.coordination.lifecycle_helpers import open_collaboration, select_review
from tests.coordination.publication_helpers import publish_batch

NOTE_PATH = "wiki/status.md"


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             actor or Actor(role=Role.OPERATOR))


async def quiet(hub, attempt):
    return await command(hub, "runtime.quiescent", {
        "worker_id": attempt["worker_id"],
        "snapshot": hub.blobs.snapshot(hub.workspace(attempt["worker_id"])),
    }, Actor(role=Role.RUNNER))


async def tool(case, attempt, name, arguments):
    return await case.runner._tool(attempt, "synthetic-note-" + attempt["worker_id"],
                                   "taskhub_call", {
                                       "operation": name.removeprefix("taskhub_"),
                                       "arguments": arguments,
                                   }, uuid4().hex)


def write_note(case, text):
    path = case.hub.workspace(case.author["worker_id"]) / NOTE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    (path.parent / "README.md").write_text("# Public knowledge\n## Essential\n\n## Reference notes\n"
        "[Workflow status](status.md) — Current workflow and dependencies.\n", encoding="utf-8")


async def submit(case, **fields):
    return await tool(case, case.author, "taskhub_wiki_submit", {
        "paths": [NOTE_PATH, "wiki/README.md"], "summary": "Document the synthetic workflow status.", **fields})


async def review(case, candidate, verdict):
    reviewer = await select_review(case.hub, candidate["review_task_id"], case.run["id"])
    binding = reviewer["task"]["collaboration"]["review"]
    arguments = {key: binding[key] for key in (
        "candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")}
    arguments.update(verdict=verdict, rationale="Compared the exact wiki file with the task objective.",
                     evidence=[NOTE_PATH + ": synthetic workflow status and dependency statement."])
    if verdict == "reject":
        arguments["suggestions"] = ["State whether the synthetic workflow has a pending dependency."]
    receipt = await tool(case, reviewer, "taskhub_review", arguments)
    return reviewer, receipt


async def declare(case, candidate_id):
    return await tool(case, case.author, "taskhub_complete", {
        "task_id": case.task["id"], "revision": 1,
        "evidence": {"objective": {"artifact_ids": [candidate_id],
                                    "explanation": "The published wiki describes the workflow status."}},
        "reason": "The documented status meets the original objective."})


@pytest.fixture
async def note_project(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Local Git is required for actual wiki publication")
    config = ProjectConfig(repository=RepositoryConfig(enabled=True))
    initialize(tmp_path, config)
    clock = [10_000]
    hub = Hub(tmp_path, clock=lambda: clock[0])
    try:
        initial = await command(hub, "repository.initialize", {"snapshot": hub.blobs.put_json({
            "files": [{"path": "wiki/start.md", "sha256": hub.blobs.put(b"# Initial wiki\n")}],
        })})
        task = await command(hub, "task.publish", {"contract": {
            "title": "Document a synthetic workflow", "objective": "Document the workflow status.",
            "acceptance_policy": "general.peer@1"}})
        run = await command(hub, "run.start")
        author = await hub.next_assignment(run["id"])
        case = SimpleNamespace(hub=hub, task=task, run=run, author=author, clock=clock,
                               initial=initial, runner=WorkerRunner(hub, ScriptedHarness))
        write_note(case, "# Workflow status\nThe synthetic workflow is ready.\n")
        yield case
    finally:
        await hub.close()


def test_wiki_operation_has_bounded_authorship_and_exact_replacement_fields():
    assert {item["name"] for item in dynamic_tools()} == {"taskhub_actions", "taskhub_call"}
    schema = next(item["inputSchema"] for item in operation_tools()
                  if item["name"] == "taskhub_wiki_submit")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    base = {"paths": [NOTE_PATH], "summary": "Describe the changed workflow."}
    assert set(schema["properties"]) == {"paths", "summary", "input_refs", "coauthors", "replaces"}
    assert schema["required"] == ["paths", "summary"]
    assert validator.is_valid(base)
    assert validator.is_valid({**base, "coauthors": ["Worker 1"], "replaces": "rejected-note"})
    assert validator.is_valid({**base, "coauthors": ["Worker 1"] * 32, "input_refs": ["input"] * 200})
    for fields in ({"coauthors": ["Worker 1"] * 33}, {"coauthors": "Worker 1"},
                   {"coauthors": [""]}, {"coauthors": [1]}, {"replaces": ""},
                   {"replaces": None}, {"replaces": 1}, {"input_refs": ["input"] * 201},
                   {"paths": []}, {"paths": [NOTE_PATH] * 201}, {"content": "Inline body"}):
        assert not validator.is_valid({**base, **fields}), fields


def test_opaque_tool_objects_point_to_operation_and_semantic_references():
    schemas = {item["name"]: item["inputSchema"] for item in operation_tools()}
    for name, field in (("taskhub_complete", "evidence"),
                        ("taskhub_completion_finalize", "evidence"),
                        ("taskhub_completion_finalize", "resolution")):
        description = schemas[name]["properties"][field]["description"]
        # The model sees paths and the reference index, never the internal native map.
        assert "native.directories" not in description and "paths.reference_index" in description
        assert "operation_schemas" in description and "operation_examples" in description
    description = schemas["taskhub_review"]["properties"]["semantic_checks"]["description"]
    assert "native.directories" not in description and "paths.reference_index" in description
    assert "semantic_review_guide" in description
    assert "semantic_review_schema" in description and "semantic_review_example" in description


def test_all_standing_instructions_require_terminal_results_before_voluntary_end():
    assert TURN_END_INSTRUCTIONS in WORKER_INSTRUCTIONS
    # Decisions have no tools and read nothing: they only answer.
    assert "do not run commands, read files or use tools" in DECISION_INSTRUCTIONS
    flat = " ".join(TURN_END_INSTRUCTIONS.split())
    assert "or calling an operation that ends it, you MUST wait for and collect the terminal result" in flat
    assert "every command, tool call, and background operation you started" in flat
    assert "persist needed state" in flat
    assert "closes all execution at each turn end" in TURN_END_INSTRUCTIONS
    assert not re.findall(r"taskhub_\w+", DECISION_INSTRUCTIONS)


async def test_wiki_coauthor_alias_preserves_original_and_review_exclusions(note_project):
    case = note_project
    hub = case.hub
    await open_collaboration(hub, case.task["id"])
    coauthor = await command(hub, "worker.register")
    case.clock[0] += 1
    await command(hub, "task.claim", {"task_id": case.task["id"], "revision": 1,
        "worker_id": coauthor["id"], "run_id": case.run["id"], "plan": "Check the workflow status."})
    case.clock[0] += 1
    authored_summary = "Preserve exact authored text mentioning " + coauthor["id"]
    candidate = await submit(case, coauthors=[coauthor["display_name"]], summary=authored_summary)
    artifact = await hub.artifact(candidate["artifact_id"])
    assert artifact["candidate_binding"]["coauthors"] == [coauthor["id"]]
    assert artifact["data"]["summary"] == authored_summary
    assert artifact["status"] == "staged" and not artifact["accepted"]
    for worker_id in (case.author["worker_id"], coauthor["id"]):
        assert not await hub.db.call(lambda c: hub._can_review(
            c, candidate["review_task_id"], worker_id))
        with pytest.raises(HubError) as excluded:
            await command(hub, "task.claim", {"task_id": candidate["review_task_id"],
                "revision": 1, "worker_id": worker_id, "run_id": case.run["id"]})
        assert excluded.value.code == "REVIEWER_INELIGIBLE"
    # The review goes to its own new reviewer.
    independent = await select_review(hub, candidate["review_task_id"], case.run["id"])
    assert independent["worker_id"] not in {case.author["worker_id"], coauthor["id"]}


async def test_wiki_submission_rejects_invalid_authorship_bounds_and_stale_fences(note_project):
    case = note_project
    with pytest.raises(HubError) as invalid:
        await submit(case, coauthors=["Worker 999"])
    assert invalid.value.code == "NOT_FOUND"
    for fields in ({"coauthors": "Worker 0"}, {"coauthors": ["Worker 0"] * 33},
                   {"input_refs": ["missing"] * 201}, {"paths": [NOTE_PATH] * 201}):
        invalid = await submit(case, **fields)
        assert invalid["code"] == "INVALID_ARGUMENTS"
    stale = {**case.author, "fencing_token": case.author["fencing_token"] + 1}
    with pytest.raises(HubError) as expired:
        await tool(case, stale, "taskhub_wiki_submit", {
            "paths": [NOTE_PATH], "summary": "Unauthorized attempt.", "coauthors": ["Worker 999"]})
    assert expired.value.code == "STALE_BINDING"
    assert await case.hub.list_artifacts() == []


async def test_wiki_submission_accepts_actual_coauthor_outside_the_task(note_project):
    case = note_project
    coauthor = await command(case.hub, "worker.register")
    candidate = await submit(case, coauthors=[coauthor["display_name"]])
    binding = (await case.hub.task(candidate["review_task_id"]))["collaboration"]["review"]
    assert binding["coauthors"] == [coauthor["id"]]
    assert not await case.hub.db.call(lambda c: c.execute(
        "SELECT 1 FROM task_members WHERE task_id=? AND worker_id=?",
        (case.task["id"], coauthor["id"])).fetchone())
    assert not await case.hub.db.call(lambda c: case.hub._can_review(
        c, candidate["review_task_id"], coauthor["id"]))


async def test_wiki_replacement_rejects_invalid_ids_and_unrejected_candidates(note_project):
    case = note_project
    candidate = await submit(case)
    for predecessor in ("missing-candidate", " ", candidate["artifact_id"]):
        with pytest.raises(HubError) as invalid:
            await submit(case, replaces=predecessor)
        assert invalid.value.code == "INVALID_REPLACEMENT"
    for predecessor in ("", None, 1, []):
        invalid = await submit(case, replaces=predecessor)
        assert invalid["code"] == "INVALID_ARGUMENTS"
    artifacts = await case.hub.list_artifacts()
    assert [item["id"] for item in artifacts] == [candidate["artifact_id"]]


async def test_rejected_wiki_replacement_discharges_obligation_only_after_actual_publication(note_project):
    case = note_project
    hub = case.hub
    original = await submit(case)
    reviewer, rejected = await review(case, original, "reject")
    assert rejected["runtime_directive"] == "drain_for_review"
    await quiet(hub, reviewer)
    assert (await hub.artifact(original["artifact_id"]))["status"] == "rejected"
    with pytest.raises(HubError) as blocked:
        await declare(case, original["artifact_id"])
    assert blocked.value.code == "ARTIFACT_GATE"

    write_note(case, "# Workflow status\nThe synthetic workflow is ready. No dependency is pending.\n")
    corrected = await submit(case, replaces=original["artifact_id"])
    obligation = await hub.db.call(lambda c: dict(c.execute(
        "SELECT * FROM artifact_obligations WHERE artifact_id=?", (corrected["artifact_id"],)
    ).fetchone()))
    assert obligation["predecessor_id"] == original["artifact_id"]
    assert obligation["required"] == 1
    with pytest.raises(HubError) as repeated:
        await submit(case, replaces=original["artifact_id"])
    assert repeated.value.code == "REPLACEMENT_EXISTS"
    reviewer, accepted = await review(case, corrected, "accept")
    assert accepted["state"] == "integrating"
    assert not (await hub.artifact(corrected["artifact_id"]))["accepted"]
    with pytest.raises(HubError) as blocked:
        await declare(case, corrected["artifact_id"])
    assert blocked.value.code == "ARTIFACT_GATE"

    await hub.ordinary_turn_completed(reviewer["worker_id"], reviewer["id"])
    await publish_batch(hub, case.run["id"])

    assert (await hub.artifact(original["artifact_id"]))["status"] == "rejected"
    assert (await hub.artifact(corrected["artifact_id"]))["status"] == "accepted"
    publication = await hub.db.call(lambda c: dict(c.execute(
        "SELECT * FROM repository_publications WHERE candidate_id=?", (corrected["artifact_id"],)
    ).fetchone()))
    assert publication["commit_sha"] == (await hub.repository_status())["head"]
    assert publication["commit_sha"] != case.initial["head"]
    declaration = await declare(case, corrected["artifact_id"])
    finalized = await tool(case, case.author, "taskhub_completion_finalize", {
        "declaration_id": declaration["id"], "decision": "close",
        "reason": "The corrected wiki passed independent review and was published."})
    assert finalized["state"] == "finalizing"
    await quiet(hub, case.author)
    assert (await hub.task(case.task["id"]))["state"] == "done"



async def test_one_public_note_waits_for_review_then_admits_without_batch_timer(note_project):
    case = note_project
    hub = case.hub
    await hub.db.call(hub._enable_integrator, write=True)
    candidate = await submit(case)
    assert await hub.db.call(lambda c: hub._batch_assignment(c, case.run["id"], None), write=True) is None
    reviewer, _ = await review(case, candidate, "accept")
    await hub.ordinary_turn_completed(reviewer["worker_id"], reviewer["id"])
    before = case.clock[0]
    waiting = await hub.db.call(hub._batch_waiting)
    assert waiting["count"] == 1 and waiting["documents"] == 1  # A waiting document makes the batch due now.
    assignment = await hub.db.call(lambda c: hub._batch_assignment(c, case.run["id"], None), write=True)
    assert assignment is not None and case.clock[0] == before
    batch = await hub.db.call(lambda c: hub._batch(c))
    assert batch["content_kind"] == "public-notes"
    assert [item["candidate_id"] for item in batch["items"]] == [candidate["artifact_id"]]
    assert await hub.db.call(lambda c: hub._batch_assignment(c, case.run["id"], None), write=True) is None


async def test_same_submission_entry_accepts_notes_folder(note_project):
    case = note_project
    workspace = case.hub.workspace(case.author["worker_id"])
    (workspace / "notes").mkdir(exist_ok=True)
    (workspace / "notes/approach.md").write_text("# Approach\nUseful context and limitations.\n", encoding="utf-8")
    (workspace / "wiki/README.md").write_text("# Public knowledge\n## Essential\n\n## Reference notes\n"
        "[Approach](../notes/approach.md) — A reusable approach.\n", encoding="utf-8")
    submitted = await tool(case, case.author, "taskhub_wiki_submit", {
        "paths": ["notes/approach.md", "wiki/README.md"], "summary": "Share an approach with its README entry."})
    record = await case.hub.artifact(submitted["artifact_id"])
    assert record["kind"] == "note.wiki" and record["status"] == "staged" and not record["accepted"]
    assert record["data"]["paths"] == ["notes/approach.md", "wiki/README.md"]
