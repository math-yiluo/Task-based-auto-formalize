"""Native, on-demand semantic review formats derived from the validation model."""
from __future__ import annotations

from copy import deepcopy

from .coordination.integration_notes import SCHEMA as INTEGRATION_NOTES
from .schemas import SemanticReviewEvidence


def semantic_review_schema() -> dict:
    """Describe review operation arguments without moving acceptance into JSON Schema."""
    evidence = SemanticReviewEvidence.model_json_schema()
    definitions = evidence.pop("$defs", {})
    location_rule = ("Use the exact submitted paired path followed by :line or :start-end; lines are positive integers. "
                     "Locations are in the candidate's primary pair, never another file or an imported module "
                     "(name those in the finding text); one line or one range per location, another finding "
                     "for each further range.")
    for name in ("CorrespondenceFinding", "SourceCorrespondenceFinding"):
        for field, suffix in (("lean_location", "lean"), ("markdown_location", "md")):
            definitions[name]["properties"][field].update(
                pattern=rf"^.+\.{suffix}:[1-9][0-9]*(?:-[1-9][0-9]*)?$",
                description=location_rule,
            )
        definitions[name]["properties"]["finding"]["pattern"] = r"\S"
    definitions["SourceCorrespondenceFinding"]["properties"]["content_sha256"]["pattern"] = "^[0-9a-f]{64}$"
    evidence["properties"]["source_checks"]["description"] = (
        "For definitions/statements, cover every exact submitted source tuple "
        "(literature_id, content_sha256, locator), "
        "including its edition and locator. Read the original and state concrete three-way findings."
    )
    evidence["properties"]["provenance_rationale"]["description"] = (
        "Required and nonblank for source-free definitions/statements: explain the checked derived/internal rationale."
    )
    text = {"type": "string", "minLength": 1}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "review operation semantic report",
        "description": "Format reference only. The backend binds findings to the exact candidate, paired paths, citations and versions.",
        "type": "object", "additionalProperties": False,
        "required": ["candidate_id", "candidate_hash", "task_revision", "input_refs",
                     "policy", "verdict", "rationale", "evidence"],
        "properties": {
            "candidate_id": text,
            "candidate_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "task_revision": {"type": "integer", "minimum": 1},
            "input_refs": {"type": "array", "items": text},
            "policy": text, "verdict": {"enum": ["accept", "reject"]},
            "rationale": text, "evidence": {"type": "array", "items": text},
            "qualifications": {"type": "array", "items": text},
            "suggestions": {"type": "array", "minItems": 1, "items": text},
            "semantic_checks": evidence,
            "integration_notes": deepcopy(INTEGRATION_NOTES),
        },
        "allOf": [
            {"if": {"properties": {"verdict": {"const": "accept"}}},
             "then": {"required": ["semantic_checks"],
                      "properties": {"qualifications": {"maxItems": 0}, "evidence": {"minItems": 1}}}},
            {"if": {"properties": {"verdict": {"const": "reject"}}},
             "then": {"required": ["suggestions"], "not": {"required": ["integration_notes"]}}},
        ],
        "$defs": definitions,
    }


def semantic_review_example() -> dict:
    """Minimal source-backed statement example with format-only synthetic identities."""
    finding = {
        "lean_location": "Project/Statement.lean:4-8",
        "markdown_location": "Project/Statement.md:3-6",
        "finding": "The same objects, hypotheses, quantifiers and conclusion occur in the proposition definition and its explanation.",
    }
    return {
        "candidate_id": "candidate-id", "candidate_hash": "1" * 64,
        "task_revision": 1, "input_refs": ["environment-id"],
        "policy": "lean.statement@1", "verdict": "accept",
        "rationale": "The exact original passage, paired statement files and environment correspond.",
        "evidence": ["Project/Statement.lean:4-8", "Project/Statement.md:3-6"],
        "qualifications": [],
        "semantic_checks": {
            "source_checks": [{
                **finding, "literature_id": "literature-id", "content_sha256": "2" * 64,
                "locator": "Specified edition, Definition 2.1, page 7",
                "finding": "The original definition's domain, conventions and quantified conclusion match the Markdown passage and Lean proposition definition.",
            }],
            "lean_markdown": [finding],
            "provenance_rationale": "",
        },
    }
