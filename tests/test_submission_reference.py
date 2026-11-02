"""File-based format discovery without sending domain schemas in every catalog."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime.actions import ActionCatalog, ActionError
from formal_geyser.coordination.runtime.tools import operation_tools
from formal_geyser.publication import validate_semantic_review
from formal_geyser.registry import RegistryError
from formal_geyser.schemas import SemanticReviewEvidence, SourceLocation
from formal_geyser.semantic_review_reference import (
    semantic_review_example,
    semantic_review_schema,
)
from formal_geyser.submission_reference import (
    WORKER_RECORD_TYPES,
    prepare_reference,
    submission_schema,
)


@pytest.mark.parametrize("kind", WORKER_RECORD_TYPES)
def test_reference_schemas_are_self_contained_and_resolve_their_definitions(kind):
    schema = submission_schema(kind)
    Draft202012Validator.check_schema(schema)
    # Validating an incomplete manifest walks its local data refs. Missing
    # author fields are ordinary validation errors, never unresolved $refs.
    errors = list(Draft202012Validator(schema).iter_errors({
        "kind": "lean." + kind, "data": {}, "introduction": "Review this contribution."}))
    assert errors and all(error.validator == "required" for error in errors)


def test_manifest_reference_distinguishes_authored_and_derived_fields():
    schema = submission_schema("definition")
    Draft202012Validator(schema).validate({
        "kind": "lean.definition", "introduction": "Begin with the exact paired source files.",
        "data": {"name": "Synthetic record", "entry_module": "Fixture", "declaration": "fixture",
                 "description": "Synthetic schema fixture.", "provenance": "internal",
                 "provenance_rationale": "Test the software boundary, with no mathematical claim."},
    })
    for name in ("source", "specification", "statement-binding", "environment", "proof-candidate"):
        with pytest.raises(ValueError, match="not available"):
            submission_schema(name)


def test_publication_takes_only_the_validated_round_without_semantic_reconfirmation():
    from formal_geyser.coordination.runtime.integrator import integrator_operations
    catalog = ActionCatalog(operation_tools() + integrator_operations())
    assert catalog.resolve("batch_publish", {"round_id": "exact-round"},
                           capability="integrator")[0] == "taskhub_batch_publish"
    with pytest.raises(ActionError):
        catalog.resolve("batch_publish", {"round_id": "exact-round", "reports_path": "final-confirmations.json"},
                        capability="integrator")
    with pytest.raises(ActionError) as denied:
        catalog.resolve("batch_publish", {"round_id": "exact-round"}, capability="reviewer")
    assert denied.value.code == "OPERATION_NOT_ALLOWED"
    assert not any(tool["name"].startswith("taskhub_merge_") for tool in operation_tools())


def test_new_statement_manifest_does_not_advertise_or_accept_retired_specification_linkage():
    schema = submission_schema("statement")
    data = {"name": "Synthetic statement", "entry_module": "Fixture", "declaration": "fixture",
            "statement": "Schema-only proposition.",
            "provenance": "internal", "provenance_rationale": "Synthetic authoring format check."}
    value = {"kind": "lean.statement", "introduction": "Check the exact paired source.", "data": data}
    validator = Draft202012Validator(schema)
    validator.validate(value)
    assert "specification_artifact" not in schema["properties"]["data"]["properties"]
    for retired in ("specification_artifact", "target_type_sha256", "definitions"):
        errors = list(validator.iter_errors({**value, "data": {**data, retired: "historical-record"}}))
        assert errors and errors[0].validator == "additionalProperties"


def test_proof_manifest_names_its_statement_without_artifact_bindings():
    schema = submission_schema("proof")
    fields = schema["properties"]["data"]["properties"]
    assert "statement_declaration" in fields
    assert not {"native_binding", "binding_artifact", "statement_artifact", "statement_version",
                "definitions"} & fields.keys()
    assert "NativeProofBinding" not in schema.get("$defs", {})


def test_semantic_report_example_uses_existing_model_and_exact_backend_checks(proof_case):
    hub, _, _ = proof_case
    schema, example = semantic_review_schema(), semantic_review_example()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)
    SemanticReviewEvidence.model_validate(example["semantic_checks"])
    record = deepcopy(hub.artifacts["statement-1"]["data"])
    record.update(entry_module="Project.Statement", declaration="Project.selectedStatement",
                  environment_artifact="environment-id", provenance="source",
                  sources=[{key: example["semantic_checks"]["source_checks"][0][key]
                            for key in ("literature_id", "content_sha256", "locator")}])
    record["bundle"].update(lean_path="Project/Statement.lean", markdown_path="Project/Statement.md")
    report = {**example, "verdict": "approve"}
    validate_semantic_review(record, report)
    wrong_source = deepcopy(report)
    wrong_source["semantic_checks"]["source_checks"][0]["content_sha256"] = "3" * 64
    with pytest.raises(RegistryError, match="every exact cited"):
        validate_semantic_review(record, wrong_source)
    wrong_pair = deepcopy(report)
    wrong_pair["semantic_checks"]["lean_markdown"][0]["lean_location"] = "Other/Statement.lean:4-8"
    with pytest.raises(RegistryError, match="Semantic review locations are Project/Statement.lean:<line>"):
        validate_semantic_review(record, wrong_pair)


@pytest.mark.parametrize("location", ["Project.selectedStatement", "Project/Statement.lean",
                                      "Project/Statement.lean:0", "Project/Statement.lean:4-0"])
def test_semantic_schema_explains_concrete_line_locations(location):
    example = semantic_review_example()
    example["semantic_checks"]["lean_markdown"][0]["lean_location"] = location
    assert list(Draft202012Validator(semantic_review_schema()).iter_errors(example))


@pytest.mark.parametrize("change", [
    {"literature_id": None}, {"content_sha256": None}, {"content_sha256": "invented"},
    {"source_artifact": "historical-source", "source_version": 1}, {"finding": "  "},
])
def test_semantic_reference_rejects_incomplete_or_mixed_citation_identities(change):
    example = semantic_review_example()
    example["semantic_checks"]["source_checks"][0].update(change)
    assert list(Draft202012Validator(semantic_review_schema()).iter_errors(example))


def test_semantic_report_reference_requires_findings_and_preserves_unresolved_checks():
    schema = Draft202012Validator(semantic_review_schema())
    example = semantic_review_example()
    del example["semantic_checks"]
    assert list(schema.iter_errors(example))
    example = semantic_review_example()
    example["qualifications"] = ["The original passage was inaccessible."]
    assert list(schema.iter_errors(example))
    example.update(verdict="reject", suggestions=["Correct the mismatched quantifier at the cited line."])
    del example["semantic_checks"]
    schema.validate(example)


def test_semantic_reference_takes_integration_notes_with_an_acceptance_only():
    reference = semantic_review_schema()
    schema = Draft202012Validator(reference)
    example = semantic_review_example()
    example["integration_notes"] = {
        "overlaps": [{"existing": "Project.relatedStatement", "relation": "related",
                      "note": "The same objects with a different conclusion; neither replaces the other."}],
        "placement": "Project/, beside the related statement."}
    schema.validate(example)
    for notes in ({}, {"placement": ""}, {"overlaps": [{"existing": "Project.x", "relation": "same", "note": "n"}]}):
        assert list(schema.iter_errors({**example, "integration_notes": notes}))
    rejected = {**example, "verdict": "reject", "suggestions": ["Reuse the related statement."]}
    del rejected["semantic_checks"]
    assert list(schema.iter_errors(rejected))
    # The review operation itself offers the same notes format.
    review = next(spec for spec in operation_tools() if spec["name"] == "taskhub_review")
    assert review["inputSchema"]["properties"]["integration_notes"] == reference["properties"]["integration_notes"]


async def test_worker_reference_is_readable_idempotent_and_absent_from_candidate_snapshot(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig(domain="lean"))
    try:
        worker = await hub.execute(Command(name="worker.register", key="reference-worker", payload={}),
                                   Actor(role=Role.OPERATOR))
        workspace = hub.workspace(worker["id"])
        old_schema = workspace / ".private/taskhub-reference/schemas/source.json"
        old_schema.parent.mkdir(parents=True)
        old_schema.write_text('{"legacy": true}', encoding="utf-8")
        paths = prepare_reference(workspace)
        guide = workspace / paths["submission_guide"]
        schemas = workspace / paths["submission_schemas"]
        text = guide.read_text(encoding="utf-8")
        assert 'taskhub_call(operation="stage"' in text
        assert "taskhub_stage" not in text
        assert "original" in guide.read_text(encoding="utf-8")
        assert {p.stem for p in schemas.glob("*.json")} == set(WORKER_RECORD_TYPES)
        assert set(WORKER_RECORD_TYPES) == {"definition", "statement", "proof"}
        assert schemas.name == "native" and old_schema.read_text() == '{"legacy": true}'
        assert json.loads((schemas / "definition.json").read_text()) == submission_schema("definition")
        assert json.loads((workspace / paths["semantic_review_schema"]).read_text()) == semantic_review_schema()
        assert json.loads((workspace / paths["semantic_review_example"]).read_text()) == semantic_review_example()
        assert not list(workspace.rglob("final-semantic-*.json"))
        identity = [name for name, field in SourceLocation.model_fields.items() if field.is_required()]
        assert f"`({', '.join(identity)})`" in (workspace / paths["semantic_review_guide"]).read_text(encoding="utf-8")
        before = {p: p.stat().st_mtime_ns for p in guide.parent.rglob("*") if p.is_file()}
        assert prepare_reference(workspace) == paths
        assert {p: p.stat().st_mtime_ns for p in before} == before
        (workspace / "candidate.md").write_text("# Selected note\n", encoding="utf-8")
        snapshot = hub._candidate_snapshot(worker["id"])
        assert [item["path"] for item in hub.blobs.get_json(snapshot)["files"]] == ["candidate.md"]
    finally:
        await hub.close()
