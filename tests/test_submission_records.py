"""Native submission normalization boundary checks, with no Lean or model execution."""
from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from formal_geyser.coordination.models import TaskContract
from formal_geyser.registry import RegistryError
from formal_geyser.submission_records import normalize_submission_record
from tests.conftest import FakeHub, snapshot


def manifest(task, *, kind, data, paths=None, refs=None):
    return {"task_id": task["id"], "task_revision": task["revision"],
            "kind": kind, "data": data, "repository_paths": paths or [], "input_refs": refs or []}


def minimal_proof(task, artifact):
    return manifest(task, kind="lean.proof", data={
        key: artifact["data"][key] for key in ("name", "entry_module", "declaration", "statement_declaration")})


def peer_task():
    contract = TaskContract(title="Synthetic review", objective="Exercise a software protocol.",
                            domain="lean", acceptance_policy="lean.peer@1")
    return {"id": "task-1", "revision": 1, "contract": contract.model_dump(mode="json")}


async def test_native_proof_fills_only_pinned_fields_and_authoritative_hashes(proof_case):
    hub, task, artifact = proof_case
    payload = minimal_proof(task, artifact)
    original_payload, original_artifacts = deepcopy(payload), deepcopy(hub.artifacts)
    result = await normalize_submission_record(hub, task, payload, artifact["snapshot"])
    assert result["data"] == artifact["data"]
    assert result["policy"] == "lean.proof@1"
    assert result["input_refs"] == ["environment-1"]
    assert result["repository_paths"] == ["Candidate.lean", "Candidate.md"]
    assert "base_commit" not in result
    assert payload == original_payload and hub.artifacts == original_artifacts
    assert hub.commands == []
    assert "accepted" not in result and "evidence" not in result


@pytest.mark.parametrize("field,value", [
    ("environment_artifact", "other-environment"), ("environment_version", 2),
    ("task_id", "another-task"), ("task_revision", 3), ("snapshot", "0" * 64),
])
async def test_native_proof_rejects_conflicting_pinned_claims(proof_case, field, value):
    hub, task, artifact = proof_case
    payload = minimal_proof(task, artifact)
    payload["data"][field] = value
    with pytest.raises(RegistryError, match="authoritative value"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


@pytest.mark.parametrize("field,value", [
    ("snapshot", "0" * 64), ("lean_sha256", "0" * 64), ("markdown_sha256", "0" * 64),
    ("lean_path", "Other.lean"), ("markdown_path", "Other.md"),
])
async def test_native_bundle_never_repairs_supplied_hashes_or_paths(proof_case, field, value):
    hub, task, artifact = proof_case
    payload = minimal_proof(task, artifact)
    payload["data"]["bundle"] = {field: value}
    with pytest.raises(RegistryError, match="authoritative value"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


@pytest.mark.parametrize("field,value", [("task_id", "another-task"), ("task_revision", 1)])
async def test_native_manifest_cannot_move_to_a_new_task_revision(proof_case, field, value):
    hub, task, artifact = proof_case
    payload = minimal_proof(task, artifact)
    payload[field] = value
    with pytest.raises(RegistryError, match="authoritative value"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


async def test_native_proof_requires_both_real_source_files(proof_case):
    hub, task, artifact = proof_case
    incomplete = hub.blobs.put_json({"files": [
        {"path": "Candidate.lean", "sha256": hub.blobs.put(b"-- Synthetic source only.\n")} ]})
    with pytest.raises(RegistryError, match="same-stem"):
        await normalize_submission_record(hub, task, minimal_proof(task, artifact), incomplete)


async def test_native_build_retains_exact_module_order_and_requires_pairs(build_case):
    hub, task, artifact = build_case
    payload = manifest(task, kind="lean.build-candidate", data={"name": "Synthetic build"})
    result = await normalize_submission_record(hub, task, payload, artifact["snapshot"])
    assert result["data"]["modules"] == ["Candidate"]
    assert result["data"]["snapshot"] == artifact["snapshot"]
    assert result["policy"] == "lean.build@1"
    assert result["repository_paths"] == []
    payload["data"]["modules"] = ["Other"]
    with pytest.raises(RegistryError, match="modules"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


async def test_native_statement_does_not_invent_provenance_or_proposition_fingerprint(proof_case):
    hub, _, artifact = proof_case
    task = peer_task()
    task["contract"]["inputs"] = ["environment-1"]
    data = {"name": "Synthetic statement", "entry_module": "Candidate", "declaration": "Candidate.target",
            "statement": "Software fixture only.", "provenance": "internal",
            "provenance_rationale": "Synthetic protocol."}
    payload = manifest(task, kind="lean.statement", data=data)
    result = await normalize_submission_record(hub, task, payload, artifact["snapshot"])
    assert result["data"]["environment_version"] == 1
    assert "target_type_sha256" not in result["data"]
    assert result["policy"] == "lean.statement@1"
    data["target_type_sha256"] = "b" * 64
    with pytest.raises(ValidationError, match="target_type_sha256"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])
    data.pop("target_type_sha256")
    data["provenance"] = "source"
    data.pop("provenance_rationale")
    with pytest.raises(ValidationError, match="source references"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


async def test_native_environment_default_must_be_unambiguous(proof_case):
    hub, _, artifact = proof_case
    task = peer_task()
    payload = manifest(task, kind="lean.definition", data={
        "name": "Synthetic definition", "entry_module": "Candidate", "declaration": "Candidate.fixture",
        "description": "Software fixture.", "provenance": "internal", "provenance_rationale": "Synthetic."})
    with pytest.raises(RegistryError, match="unambiguous"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])
    hub.artifacts["environment-2"] = deepcopy(hub.artifacts["environment-1"])
    task["contract"]["inputs"] = ["environment-1", "environment-2"]
    with pytest.raises(RegistryError, match="unambiguous"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


async def test_native_generic_work_uses_peer_policy_and_preserves_paths_and_refs(proof_case):
    hub, task, artifact = proof_case
    payload = manifest(task, kind="workload.observation", data={"summary": "Synthetic observation."},
                       paths=["Notes.md", "Deleted.md"], refs=["specification-1"])
    result = await normalize_submission_record(hub, task, payload, artifact["snapshot"])
    assert result["policy"] == "lean.peer@1"
    assert result["repository_paths"] == payload["repository_paths"]
    assert result["input_refs"] == ["environment-1", "specification-1"]
    assert "accepted" not in result
    payload["policy"] = "lean.proof@1"
    with pytest.raises(RegistryError, match="policy"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


@pytest.mark.parametrize("claim", [
    {"accepted": True}, {"proof_accepted": True}, {"compilation_verified": True},
    {"environment_authorization": True}, {"subject_approval": True},
    {"receipt": {"verdict": "accepted"}}, {"checks": [{"verdict": "verified"}]},
])
async def test_native_generic_work_cannot_claim_authoritative_acceptance(tmp_path, claim):
    hub, task = FakeHub(tmp_path), peer_task()
    sha = hub.blobs.put_json({"files": []})
    with pytest.raises(RegistryError, match="acceptance|authority"):
        await normalize_submission_record(
            hub, task, manifest(task, kind="workload.observation", data=claim), sha)


@pytest.mark.parametrize("source", [b"def fixture := sorry\n", b"axiom fixture : Prop\n"])
async def test_native_generic_work_cannot_bypass_hole_checks(tmp_path, source):
    hub, task = FakeHub(tmp_path), peer_task()
    sha = snapshot(hub, "Fixture.lean", source)
    with pytest.raises(RegistryError, match="prohibited"):
        await normalize_submission_record(
            hub, task, manifest(task, kind="workload.observation", data={"summary": "Synthetic."}), sha)


async def test_native_control_files_cannot_create_self_referential_source_snapshots(tmp_path):
    hub, task = FakeHub(tmp_path), peer_task()
    sha = hub.blobs.put_json({"files": [{"path": ".taskhub-contributions/id/contribution.json",
                                        "sha256": hub.blobs.put(b"{}")} ]})
    with pytest.raises(RegistryError, match="exclude contribution control"):
        await normalize_submission_record(
            hub, task, manifest(task, kind="workload.observation", data={}), sha)


@pytest.mark.parametrize("paths", [["../outside"], ["A.md", "a.md"],
                                   [".taskhub-contributions/id/contribution.json"]])
async def test_native_publication_paths_reject_escape_collision_or_controls(tmp_path, paths):
    hub, task = FakeHub(tmp_path), peer_task()
    sha = hub.blobs.put_json({"files": []})
    with pytest.raises(ValueError):
        await normalize_submission_record(
            hub, task, manifest(task, kind="workload.observation", data={}, paths=paths), sha)


async def test_native_environment_registration_still_requires_operator_authority(proof_case):
    hub, task, artifact = proof_case
    payload = manifest(task, kind="lean.environment", data=hub.artifacts["environment-1"]["data"])
    with pytest.raises(RegistryError, match="operator authority"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])


async def test_native_native_record_cannot_downgrade_to_generic_review(proof_case):
    hub, task, artifact = proof_case
    payload = manifest(task, kind="workload.observation", data=artifact["data"])
    with pytest.raises(RegistryError, match="reserved Lean record"):
        await normalize_submission_record(hub, task, payload, artifact["snapshot"])
