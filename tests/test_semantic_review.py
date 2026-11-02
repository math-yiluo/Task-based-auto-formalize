"""Compact independent review findings bind the real pair and cited locations."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from formal_geyser.publication import validate_semantic_review
from formal_geyser.registry import RegistryError


def proof_evidence():
    return {"verdict": "approve", "semantic_checks": {
        "lean_markdown": [{"lean_location": "Candidate.lean:1-4", "markdown_location": "Candidate.md:1",
                           "finding": "The explanation follows the implemented proof and its exact target."}]}}


def test_proof_review_requires_located_pair_findings(proof_case):
    _, _, artifact = proof_case
    validate_semantic_review(artifact["data"], proof_evidence())
    retired = proof_evidence()
    retired["semantic_checks"]["dependencies"] = [
        {"artifact_id": "environment-1", "version": 1, "finding": "Retired dependency finding."}]
    with pytest.raises(ValidationError):
        validate_semantic_review(artifact["data"], retired)


def test_math_approval_cannot_use_only_an_unlocated_narrative(proof_case):
    _, _, artifact = proof_case
    with pytest.raises(ValidationError):
        validate_semantic_review(artifact["data"], {"verdict": "approve", "evidence": ["Looks correct."]})
    report = proof_evidence()
    report["semantic_checks"]["lean_markdown"][0]["lean_location"] = "Different.lean:1"
    with pytest.raises(RegistryError, match="Semantic review locations are Candidate.lean:<line>"):
        validate_semantic_review(artifact["data"], report)
    # Reviewers cited imported modules, or several ranges in one location, and resubmitted.
    report["semantic_checks"]["lean_markdown"][0]["lean_location"] = "Candidate.lean:2-3,5-6"
    with pytest.raises(RegistryError, match="name any other file or imported module in the finding text, "
                                            "and give each further range its own finding"):
        validate_semantic_review(artifact["data"], report)


def test_source_statement_needs_every_exact_three_way_correspondence(proof_case):
    hub, _, _ = proof_case
    statement = deepcopy(hub.artifacts["statement-1"]["data"])
    statement.update(provenance="source", sources=[{"literature_id": "literature-1", "content_sha256": "3" * 64,
                                                   "locator": "Definition 2.1, page 7"}])
    report = {"verdict": "approve", "semantic_checks": {
        "lean_markdown": [{"lean_location": "Baseline.lean:1", "markdown_location": "Baseline.md:1",
                           "finding": "The proposition parameters and quantifiers correspond."}],
        "source_checks": [{"literature_id": "literature-1", "content_sha256": "3" * 64,
                           "locator": "Definition 2.1, page 7", "lean_location": "Baseline.lean:1",
                           "markdown_location": "Baseline.md:1",
                           "finding": "The source's assumptions, definitions and conclusion match both representations."}]}}
    validate_semantic_review(statement, report)
    report["semantic_checks"]["source_checks"][0]["locator"] = "Definition 2.2, page 8"
    with pytest.raises(RegistryError, match="every exact cited"):
        validate_semantic_review(statement, report)


def test_rejection_does_not_require_approval_findings(proof_case):
    _, _, artifact = proof_case
    validate_semantic_review(artifact["data"], {"verdict": "reject", "rationale": "The stated quantifier differs at line 3."})
