"""Current authoring boundaries retain historical read evidence without live writes."""
from __future__ import annotations

import pytest

from formal_geyser.coordination.models import Actor, Role, TaskContract
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import ObjectRegistry, RegistryError
from formal_geyser.schemas import EnvironmentRecord
from formal_geyser.verification import ProofVerifier
from tests.conftest import FakeHub


def metadata_task(policy):
    contract = TaskContract(title="Synthetic metadata review", objective="Inspect a software fixture.",
                            domain="lean", acceptance_policy=policy)
    return {"id": "task-1", "revision": 1, "contract": contract.model_dump(mode="json"),
            "collaboration": {"mode": "collaborative"}}


@pytest.mark.parametrize("kind", ["source", "specification", "statement-binding", "obligation", "review"])
async def test_retired_record_kinds_are_rejected_before_writes(tmp_path, kind):
    hub = FakeHub(tmp_path)
    with pytest.raises(ValueError):
        await ObjectRegistry(hub).stage({"object_type": kind, "name": "Retired"}, Actor(role=Role.OPERATOR))
    assert not hub.commands


@pytest.mark.parametrize("policy", ["lean.source@1", "lean.specification@1", "lean.binding@1", "lean.obligation@1", "lean.review@1"])
def test_obsolete_policies_cannot_create_current_tasks(policy):
    with pytest.raises(ValueError, match="Unknown Lean acceptance policy"):
        LeanPlugin().validate_contract(TaskContract.model_validate(metadata_task(policy)["contract"]))


async def test_publication_rejects_mismatched_kind_and_environment_authority(proof_case):
    hub, task, artifact = proof_case
    contract = TaskContract.model_validate(task["contract"])
    with pytest.raises(RegistryError, match="kind"):
        LeanPlugin().validate_publication(contract, "lean.source", artifact["data"], "lean.source@1")
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"])
    with pytest.raises(RegistryError, match="operator authority"):
        LeanPlugin().validate_publication(contract, "lean.environment", environment.model_dump(mode="json"), "lean.environment@1")


async def test_peer_evidence_cannot_enable_disabled_compilation(proof_case):
    hub, task, artifact = proof_case
    hub.artifacts["environment-1"]["data"]["local_execution"] = "disabled"
    artifact["evidence"] = [{"policy": "task-based.peer-review@1", "verdict": "accepted"}]
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.policy == "lean.proof@1"
    assert report.verdict == "unavailable"
    assert report.details["proof_accepted"] is False


async def test_generic_peer_work_product_has_no_proof_or_compiler_authority(tmp_path):
    hub = FakeHub(tmp_path)
    artifact = {"id": "summary", "kind": "workload.milestone", "data": {"summary": "Software fixture"}}
    report = await LeanPlugin().validate_candidate(hub, metadata_task("lean.peer@1"), artifact)
    assert report.verdict == "accepted"
    assert report.details["proof_accepted"] is False
    assert report.details["compilation_verified"] is False
    for field in ("kind", "data"):
        reserved = {**artifact, field: "lean.proof-candidate" if field == "kind" else {"object_type": "proof-candidate"}}
        assert (await LeanPlugin().validate_candidate(hub, metadata_task("lean.peer@1"), reserved)).verdict == "rejected"


@pytest.mark.parametrize("discriminator", ["proof-candidate", ["proof-candidate"], {"kind": "proof-candidate"}])
async def test_generic_peer_policy_rejects_domain_discriminators_without_type_errors(tmp_path, discriminator):
    artifact = {"id": "summary", "kind": "workload.milestone", "data": {"object_type": discriminator}}
    report = await LeanPlugin().validate_candidate(FakeHub(tmp_path), metadata_task("lean.peer@1"), artifact)
    assert report.verdict == "rejected"
