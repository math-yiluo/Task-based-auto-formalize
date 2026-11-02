"""Lake-backed proof admission retains immutable targets and independent peer review."""

from copy import deepcopy

import pytest

from formal_geyser.verification import ProofVerifier
from tests.conftest import FakeRunner


async def test_lake_proof_evidence(proof_case, lean_service):
    hub, task, artifact = proof_case
    runner = lean_service.runner = FakeRunner()
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "accepted", report.details
    assert report.details["proof_accepted"] and report.details["peer_review_required"]
    assert report.details["validation_method"] == "lake-build"
    assert set(report.details["checked_modules"]) == {"Baseline", "Candidate"}
    assert "exact-target" in report.details["checks"]
    assert not {"kernel_replay", "snapshot_axiom_audit"}.intersection(report.details["checks"])
    assert len([a for a, _ in runner.calls if "build" in a]) == 1
    assert not hub.commands
    # One official check builds the frozen snapshot and inspects the theorem against its statement.
    [spec] = lean_service.specs
    assert (spec.lane, spec.label) == ("official", "Proof candidate candidate-1")
    assert (spec.request.snapshot, spec.request.kernel_replay) == (artifact["snapshot"], True)
    assert [item.model_dump() for item in spec.request.inspections] == [
        {"module": "Candidate", "declaration": "Candidate.target", "statement": "Baseline.target"}]
    inspection = report.details["candidate_inspection"]
    assert (inspection["module"], inspection["declaration"]) == ("Candidate", "Candidate.target")


@pytest.mark.parametrize(
    "field,value",
    [("task_revision", 1), ("environment_artifact", "environment-2"), ("snapshot", "0" * 64)],
)
async def test_changed_candidate_binding_is_rejected(proof_case, lean_service, field, value):
    hub, task, artifact = proof_case
    artifact["data"][field] = value
    runner = lean_service.runner = FakeRunner()
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "rejected"
    assert not runner.calls and not lean_service.specs


async def test_required_approval_is_retained(proof_case):
    hub, task, artifact = proof_case
    hub.artifacts["environment-1"]["evidence"] = []
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "needs_operator"


@pytest.mark.parametrize(
    "mutation", ["wrong_target", "wrong_kind", "statement_outside_base", "missing_target"]
)
async def test_compilation_alone_cannot_bypass_target_checks(proof_case, lean_service, mutation):
    hub, task, artifact = proof_case

    def mutate(metadata, _):
        if metadata["module"] == "Candidate":
            if mutation == "wrong_target":
                metadata["target_matches"] = False
            if mutation == "wrong_kind":
                metadata["kind"] = "definition"
            if mutation == "missing_target":
                metadata.pop("target_matches")
            if mutation == "statement_outside_base":
                metadata["statement_module"] = "Elsewhere"

    lean_service.runner = FakeRunner(mutate)
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == ("unavailable" if mutation == "missing_target" else "rejected"), (
        report.details
    )
    assert not report.details.get("proof_accepted")


async def test_proof_cannot_change_its_statement_or_imports(proof_case):
    hub, task, artifact = proof_case
    files = [dict(item) for item in hub.blobs.get_json(artifact["snapshot"])["files"]]
    for item in files:
        if item["path"] == "Baseline.lean":
            item["sha256"] = hub.blobs.put(b"-- A weakened statement.\n")
    changed = hub.blobs.put_json({"files": files})
    artifact["snapshot"] = artifact["data"]["snapshot"] = artifact["data"]["bundle"]["snapshot"] = changed
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "rejected"
    assert "Baseline.lean" in report.details["reason"]


async def test_build_crash_is_unavailable(proof_case, lean_service):
    hub, task, artifact = proof_case
    lean_service.runner = FakeRunner(exit_code=1)
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "unavailable"


async def test_disabled_execution_and_changed_compiler_are_unavailable(proof_case, lean_service):
    hub, task, artifact = proof_case
    environment = hub.artifacts["environment-1"]["data"]
    environment["local_execution"] = "disabled"
    assert (
        await ProofVerifier().validate(hub, task, artifact)
    ).verdict == "unavailable"
    assert not lean_service.specs
    environment["local_execution"] = "trusted-code-only"
    environment["lean_executable_sha256"] = "0" * 64
    report = await ProofVerifier().validate(hub, task, artifact)
    # The Lean service checks the registered compiler before it starts any process.
    assert report.verdict == "unavailable"
    assert "executable content has changed" in report.details["reason"]
    assert len(lean_service.specs) == 1 and not lean_service.runner.calls


@pytest.mark.parametrize("name,body", [("Baseline.lean", b"changed"), ("Payload.olean", b"binary")])
async def test_protected_inputs_and_source_only_submission(proof_case, name, body):
    hub, task, artifact = proof_case
    entries = deepcopy(hub.blobs.get_json(artifact["snapshot"])["files"])
    entries.append({"path": name, "sha256": hub.blobs.put(body)})
    sha = hub.blobs.put_json({"files": entries})
    artifact["snapshot"] = artifact["data"]["snapshot"] = artifact["data"]["bundle"]["snapshot"] = (
        sha
    )
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "rejected"


@pytest.mark.parametrize("target", ["outputs", "sources"])
async def test_inspection_cannot_mutate_frozen_input(proof_case, lean_service, target):
    hub, task, artifact = proof_case

    def mutate(_, packet):
        # The inspection runs in the slot's build outputs (.lake/build/lib/lean); the slot's
        # copy of the sources is its source directory.
        directory = packet["directory"]
        if target == "sources":
            directory = directory.parents[3] / "source"
        (directory / "Baseline.lean").write_text("changed")

    lean_service.runner = FakeRunner(mutate)
    report = await ProofVerifier().validate(hub, task, artifact)
    # The Lean service compares the slot with the snapshot after the inspection.
    assert report.verdict == "rejected"
    assert "Inspection changed a frozen proof input" in report.details["reason"]


async def test_development_policy_does_not_publish(proof_case):
    hub, task, artifact = proof_case
    task["contract"]["acceptance_policy"] = "lean.development@1"
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "needs_operator" and not report.details["proof_accepted"]


