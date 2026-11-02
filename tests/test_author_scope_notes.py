"""Author scope notes stay reviewable without replacing machine/checker verdicts."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from formal_geyser.coordination.models import TaskContract
from formal_geyser.publication import validate_metadata
from tests.test_mathematical_artifacts import MetadataRunner

FAILURE = "Synthetic failed machine check."


class FailingRunner(MetadataRunner):
    """One phase of the synthetic machine check fails: the compiler cannot report its version,
    Lean reports an error in the build, or the inspector reports a finding."""

    def __init__(self, phase):
        super().__init__()
        self.phase = phase

    async def run(self, argv, **kwargs):
        result = await super().run(argv, **kwargs)
        if self.phase == "doctor" and "--version" in argv:
            return replace(result, returncode=1, stdout="")
        if self.phase == "compile" and "build" in argv:
            return replace(result, returncode=1, stdout=json.dumps(
                {"severity": "error", "pos": {"line": 1, "column": 0}, "data": FAILURE}))
        if self.phase == "inspect" and "--output" in argv:
            return replace(result, returncode=1, stdout=json.dumps({"finding": FAILURE}))
        return result


def scoped_candidate(proof_case, lean_service, kind="statement"):
    hub, _, _ = proof_case
    hub.artifacts["environment-1"]["data"]["local_execution"] = "trusted-code-only"
    lean_service.runner = MetadataRunner()
    artifact = deepcopy(hub.artifacts["statement-1"])
    artifact["data"]["qualifications"] = [
        "This definition layer leaves the general realization theorem to a later task.",
        "The documented normalization is part of the interface."]
    if kind == "definition":
        artifact["kind"] = "lean.definition"
        artifact["data"]["object_type"] = "definition"
        artifact["data"]["description"] = "Synthetic scoped definition."
        artifact["data"].pop("statement")
    contract = TaskContract(title="Scoped contribution", objective="Review the stated scope.",
                            domain="lean", acceptance_policy="lean." + kind + "@1")
    return hub, {"id": "task-1", "revision": 1, "contract": contract.model_dump(mode="json")}, artifact


@pytest.mark.parametrize("kind", ["definition", "statement"])
async def test_scope_notes_survive_original_validation(proof_case, lean_service, kind):
    hub, task, artifact = scoped_candidate(proof_case, lean_service, kind)
    original = deepcopy(artifact)
    report = await validate_metadata(hub, task, artifact)
    assert report.verdict == "accepted", report.details
    assert report.qualifications == []
    assert report.details["author_qualifications"] == artifact["data"]["qualifications"]
    assert report.details["peer_review_required"]
    assert not report.details["proof_accepted"]
    assert report.details["compilation"]["ok"]
    assert artifact == original


@pytest.mark.parametrize("phase,verdict", [("doctor", "unavailable"), ("compile", "rejected"),
                                           ("inspect", "rejected")])
async def test_scope_notes_do_not_override_failed_machine_checks(proof_case, lean_service, phase, verdict):
    hub, task, artifact = scoped_candidate(proof_case, lean_service)
    lean_service.runner = FailingRunner(phase)
    report = await validate_metadata(hub, task, artifact)
    assert report.verdict == verdict
    assert report.details["author_qualifications"] == artifact["data"]["qualifications"]
    assert report.details[phase if phase == "doctor" else {"compile": "compilation", "inspect": "inspection"}[phase]]["ok"] is False
