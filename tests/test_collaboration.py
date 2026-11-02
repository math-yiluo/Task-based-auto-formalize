"""Real collaborative metadata lifecycle; all work is synthetic and offline."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    RepositoryConfig,
    Role,
    TaskContract,
)
from formal_geyser.plugin import LeanPlugin
from formal_geyser.process import ProcessResult
from formal_geyser.registry import ObjectRegistry, RegistryError, file_sha256
from formal_geyser.schemas import EnvironmentRecord, SourceLocation
from formal_geyser.worker_tools import handle_worker_tool
from tests.bootstrap_helpers import synthetic_execution
from tests.conftest import FakeRunner
from tests.coordination.gateway_helpers import file_arguments
from tests.coordination.lifecycle_helpers import finish_ordinary, select_review
from tests.coordination.publication_helpers import approve, file_payload, new_hub


async def command(hub, name, payload, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload),
                             actor or Actor(role=Role.OPERATOR))


async def claim(hub, task, run):
    if task["contract"]["kind"] == "review":
        attempt = await select_review(hub, task["id"], run["id"])
        return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                     fencing_token=attempt["fencing_token"])
    worker = await command(hub, "worker.register", {})
    attempt = await command(hub, "task.claim", {"task_id": task["id"], "revision": task["revision"],
                                               "worker_id": worker["id"], "run_id": run["id"]})
    return Actor(role=Role.WORKER, worker_id=worker["id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


async def approve_peer(hub, staged, run, *, semantic_checks=None, publish=True):
    review = await hub.task(staged["review_task_id"])
    actor = await claim(hub, review, run)
    basis = review["collaboration"]["review"]
    payload = {key: basis[key] for key in ("candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")}
    if semantic_checks is not None:
        payload["semantic_checks"] = semantic_checks
    payload.update(verdict="approve",
                   rationale="Checked the exact immutable source and dependency policy.",
                   evidence=["Inspected candidate " + basis["candidate_hash"]])
    if publish:
        return await approve(hub, actor, payload)
    await command(hub, "review.accept", payload, actor)
    return await hub.ordinary_turn_completed(actor.worker_id, actor.attempt_id)


@pytest.fixture
async def collaborative_worker(tmp_path):
    hub = await new_hub(tmp_path, config=ProjectConfig(domain="lean"))
    synthetic_execution(hub)
    try:
        contract = TaskContract(title="Synthetic collaborative metadata", objective="Inspect software metadata.",
                                domain="lean", acceptance_policy="lean.peer@1")
        task = await command(hub, "task.publish", {"contract": contract.model_dump(mode="json")})
        run = await command(hub, "run.start", {})
        actor = await claim(hub, task, run)
        yield hub, task, run, actor
    finally:
        await hub.close()


async def test_legacy_source_alias_is_rejected_and_native_literature_is_acquisition_only(collaborative_worker):
    hub, task, _, actor = collaborative_worker
    source = hub.workspace(actor.worker_id) / "source.txt"
    source.write_text("Software source fixture.", encoding="utf-8")
    with pytest.raises(RegistryError, match="Unknown Lean worker tool"):
        await handle_worker_tool(hub, "lean_import_source", {"path": "source.txt"}, actor, key="source-stage")
    assert not await hub.list_artifacts()
    entry = await command(hub, "literature.register", {
        "source_id": "software-source-v1", "title": "Software source fixture",
        "path": str(source), "version": "Fixture edition one",
    }, actor)
    citation = SourceLocation(literature_id=entry["id"], content_sha256=entry["source_sha256"],
                              locator="Fixture edition one, line 1")
    registry = ObjectRegistry(hub)
    evidence = await registry.source_evidence(citation)
    assert evidence["content_sha256"] == file_sha256(source)
    assert evidence["scope"] == "source-acquisition-only"
    with pytest.raises(RegistryError, match="Unknown Lean worker tool"):
        await handle_worker_tool(hub, "lean_stage_record", {"record": {"object_type": "specification"}},
                                 actor, key="specification-stage")
    assert not await hub.list_artifacts()
    assert len(await hub.list_tasks()) == 1
    assert (await hub.task(task["id"]))["state"] != "done"


async def test_worker_environment_registration_is_denied_and_compiler_diagnostics_stay_private(collaborative_worker, tmp_path):
    hub, _, _, actor = collaborative_worker
    # A synthetic pinned toolchain: the tests' Lean service answers for it (FakeRunner); nothing runs it.
    runtime = tmp_path / "toolchain" / "lean"
    runtime.parent.mkdir()
    runtime.write_bytes(b"synthetic Lean executable")
    runtime.with_name("lake").write_bytes(b"synthetic Lake executable")
    record = EnvironmentRecord(name="Synthetic runtime", lean_executable=str(runtime),
                               lean_executable_sha256=file_sha256(runtime), lean_version="synthetic-version",
                               lake_executable_sha256=file_sha256(runtime.with_name("lake")),
                               local_execution="trusted-code-only")
    with pytest.raises(RegistryError, match="operator authority"):
        await ObjectRegistry(hub).stage(record, actor, key="denied-environment")
    environment = await ObjectRegistry(hub).stage(record, Actor(role=Role.OPERATOR))
    await command(hub, "artifact.accept", {"artifact_id": environment["id"], "policy": "lean.environment@1", "verdict": "accepted"})
    (hub.workspace(actor.worker_id) / "Utility.lean").write_text("-- Software metadata fixture only.\n", encoding="utf-8")
    before = len(await hub.list_tasks())
    checked = await handle_worker_tool(hub, "lean_check", {"environment_artifact": environment["id"],
                                      "modules": ["Utility"]}, actor, key="private-diagnostics")
    report = await hub.artifact(checked["diagnostic_id"])
    assert report["status"] == "private" and not report["public"] and not report["accepted"]
    assert report["input_refs"] == [environment["id"]]
    assert checked["proof_accepted"] is False
    assert len(await hub.list_tasks()) == before


async def test_runtime_validates_domain_stage_without_accepting_before_peer_review(collaborative_worker):
    from formal_geyser.coordination.runtime.runner import WorkerRunner

    hub, _, _, actor = collaborative_worker
    workspace = hub.workspace(actor.worker_id)
    (workspace / "runtime-note.md").write_text("# Runtime note\nInspect this exact source file.\n", encoding="utf-8")
    manifest = {"kind": "research.note", "data": {"summary": "Synthetic work product."},
                "introduction": "Inspect runtime-note.md.", "repository_paths": ["runtime-note.md"]}
    (workspace / "submission.json").write_text(json.dumps(manifest), encoding="utf-8")
    attempt = await hub.authorize_worker(actor)
    runner = WorkerRunner(hub)
    result = await runner._tool(attempt, "synthetic-thread", "taskhub_call",
        {"operation": "stage", "arguments": file_arguments(workspace, "stage", {
            "manifest_path": "submission.json"})}, "domain-stage")
    artifact = await hub.artifact(result["artifact_id"])
    assert artifact["publication_policy"] == "lean.peer@1"
    assert artifact["evidence"][-1]["verdict"] == "accepted"
    assert artifact["status"] == "staged" and not artifact["accepted"]
    assert artifact["evidence"][-1]["details"]["proof_accepted"] is False
    assert artifact["evidence"][-1]["details"]["compilation_verified"] is False
    assert artifact["repository"]["paths"] == ["runtime-note.md"]


async def test_generic_peer_milestone_completes_only_after_independent_review(collaborative_worker):
    hub, task, run, actor = collaborative_worker
    payload = file_payload(hub, actor, kind="workload.milestone", path="notes/milestone.json",
                           data={"summary": "Synthetic work product."})
    await command(hub, "artifact.submit", payload, actor)
    staged = await command(hub, "runtime.quiescent", {"worker_id": actor.worker_id,
                           "snapshot": hub.blobs.snapshot(hub.workspace(actor.worker_id))}, Actor(role=Role.RUNNER))
    artifact = await hub.artifact(staged["artifact_id"])
    report = await LeanPlugin().validate_candidate(hub, task, artifact)
    assert report.policy == "lean.peer@1" and report.verdict == "accepted"
    assert not report.details["proof_accepted"] and not report.details["compilation_verified"]
    await command(hub, "artifact.accept", {"artifact_id": artifact["id"], **report.model_dump(mode="json")}, Actor(role=Role.VALIDATOR))
    assert (await hub.task(task["id"]))["state"] != "done"
    await approve_peer(hub, staged, run)
    published = await hub.artifact(artifact["id"])
    assert published["accepted"]
    assert published["integration"]["state"] == "published"
    assert (await hub.task(task["id"]))["state"] != "done"
    resumed = await command(hub, "task.claim", {"task_id": task["id"], "revision": task["revision"],
                            "worker_id": actor.worker_id, "run_id": run["id"]})
    completing = Actor(role=Role.WORKER, worker_id=actor.worker_id, attempt_id=resumed["id"],
                       fencing_token=resumed["fencing_token"])
    await finish_ordinary(hub, task["id"], completing, artifact_ids=[artifact["id"]])
    await command(hub, "runtime.quiescent", {"worker_id": completing.worker_id,
                  "snapshot": hub.blobs.snapshot(hub.workspace(completing.worker_id))}, Actor(role=Role.RUNNER))
    assert (await hub.task(task["id"]))["state"] == "done"


class CompilerUnavailable(FakeRunner):
    """The registered compiler cannot start, as a synthetic executable cannot under a real runner."""

    async def run(self, argv, **kwargs):
        if "--version" in argv:
            self.calls.append((argv, kwargs))
            return ProcessResult(tuple(argv), None, "", "", 0.01, error="The synthetic compiler cannot start")
        return await super().run(argv, **kwargs)


async def test_peer_review_cannot_publish_proof_when_independent_checker_is_unavailable(tmp_path, proof_case, lean_service):
    from formal_geyser.verification import ProofVerifier

    fixture, _, _ = proof_case
    lean_service.runner = CompilerUnavailable()
    hub = Hub(tmp_path / "collaborative-proof", config=ProjectConfig(
        domain="lean", repository=RepositoryConfig(enabled=True)))
    try:
        from tests.native_publication_helpers import setup_statement
        environment, statement, run = await setup_statement(hub, fixture)
        contract = TaskContract(title="Synthetic proof protocol", objective="Check the unavailable-checker boundary.",
                                domain="lean", acceptance_policy="lean.proof@1", inputs=[environment["id"]],
                                payload={"environment_artifact": environment["id"]})
        task = await command(hub, "task.publish", {"contract": contract.model_dump(mode="json")})
        actor = await claim(hub, task, run)
        (hub.workspace(actor.worker_id) / "Candidate.lean").write_text("-- Software metadata fixture only.\n", encoding="utf-8")
        (hub.workspace(actor.worker_id) / "Candidate.md").write_text("Synthetic paired proof explanation.\n", encoding="utf-8")
        candidate_snapshot = hub.blobs.snapshot(hub.workspace(actor.worker_id))
        candidate = await LeanPlugin().normalize_submission(hub, task, {"kind": "lean.proof", "data": {
            "name": "Synthetic candidate", "entry_module": "Candidate", "declaration": "Candidate.target",
            "statement_declaration": statement["data"]["declaration"]}}, candidate_snapshot)
        await command(hub, "artifact.submit", candidate, actor)
        staged = await command(hub, "runtime.quiescent", {"worker_id": actor.worker_id,
                               "snapshot": hub.blobs.snapshot(hub.workspace(actor.worker_id))}, Actor(role=Role.RUNNER))
        artifact = await hub.artifact(staged["artifact_id"])
        report = await ProofVerifier().validate(hub, task, artifact)
        assert report.verdict == "unavailable" and report.details["proof_accepted"] is False
        assert report.details["doctor"]["ok"] is False and lean_service.specs[-1].lane == "official"
        await command(hub, "artifact.accept", {"artifact_id": artifact["id"], **report.model_dump(mode="json")}, Actor(role=Role.VALIDATOR))
        # Unavailable machine evidence is no receipt: no reviewer can start, so no
        # peer claim can stand in for it, and nothing becomes batch-eligible.
        assert not await hub.db.call(lambda c: hub._review_validated(c, staged["review_task_id"]))
        reviewer = await hub.db.call(lambda c: hub._new_worker(c, kind="review"), write=True)
        with pytest.raises(HubError) as pending:
            await command(hub, "task.claim", {"task_id": staged["review_task_id"], "revision": 1,
                                              "worker_id": reviewer, "run_id": run["id"]})
        assert pending.value.code == "VALIDATION_PENDING"
        assert await hub.db.call(hub._batch_eligible) == []
        with pytest.raises(HubError) as unverified:
            await hub.db.call(lambda c: hub._require_original_validation(c, hub._candidate_binding(c, artifact["id"])))
        assert unverified.value.code == "VALIDATION_REQUIRED"
        artifact = await hub.artifact(staged["artifact_id"])
        assert artifact["status"] == "staged" and not artifact["accepted"] and not artifact["public"]
        assert (await hub.task(task["id"]))["state"] != "done"
    finally:
        await hub.close()
