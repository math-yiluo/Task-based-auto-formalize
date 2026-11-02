"""Complete, private operation references for ordinary and domain-specific workers."""
from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from formal_geyser.coordination.runtime.files import ScopedFiles
from formal_geyser.coordination.runtime.operation_reference import (
    OPERATION_REFERENCE_DIRECTORY,
    OPERATION_SCHEMAS,
    operation_examples,
    operation_schema,
    prepare_operation_reference,
)
from formal_geyser.coordination.runtime.tools import dynamic_tools, operation_tools
from formal_geyser.coordination.storage import BlobStore


@pytest.mark.parametrize("name", OPERATION_SCHEMAS)
def test_operation_schemas_are_self_contained(name):
    Draft202012Validator.check_schema(operation_schema(name))


def test_examples_validate_against_file_schema_operation_schema_and_gateway():
    catalog = {tool["name"].removeprefix("taskhub_"): tool["inputSchema"]
               for tool in operation_tools()}
    gateway = next(tool["inputSchema"] for tool in dynamic_tools()
                   if tool["name"] == "taskhub_call")
    for name, example in operation_examples().items():
        operation = name.removesuffix("-optional-extension")
        Draft202012Validator(operation_schema(operation)).validate(example)
        Draft202012Validator(catalog[operation]).validate(example)
        Draft202012Validator(gateway).validate({"operation": operation, "arguments": example})
        Draft202012Validator(operation_schema("completion-evidence")).validate(example["evidence"])
        if "resolution" in example:
            Draft202012Validator(operation_schema("completion-resolution")).validate(example["resolution"])


@pytest.mark.parametrize("mapping", [
    {}, {"objective": "accepted-artifact-id"},
    {"objective": {"explanation": "   ", "artifact_ids": ["accepted-artifact-id"]}},
    {"objective": {"explanation": "Complete.", "artifact_ids": []}},
    {"objective": {"explanation": "Complete.", "artifact_ids": [3]}},
    {"objective": {"explanation": "Complete.", "results": [{"artifact_id": "accepted-artifact-id"}]}},
])
def test_evidence_reference_rejects_non_actionable_authored_maps(mapping):
    with pytest.raises(ValidationError):
        Draft202012Validator(operation_schema("completion-evidence")).validate(mapping)


@pytest.mark.parametrize("entry", [
    {"outcome": "approved", "explanation": "Complete.", "artifact_ids": ["result-id"]},
    {"outcome": "satisfied", "artifact_ids": ["result-id"]},
    {"outcome": "optional_extension", "explanation": "Extra scope.", "artifact_ids": []},
    {"outcome": "optional_extension", "explanation": " ", "artifact_ids": ["result-id"]},
])
def test_objection_reference_requires_outcome_explanation_and_accepted_ids(entry):
    with pytest.raises(ValidationError):
        Draft202012Validator(operation_schema("completion-resolution")).validate({"acceptance:0": entry})


def test_operation_files_are_readable_private_idempotent_and_preserve_user_notes(tmp_path):
    workspace = tmp_path / "workspace"
    notes = workspace / ".private/notes/idea.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("Private source note.\n", encoding="utf-8")
    (workspace / "public.md").write_text("Selected source.\n", encoding="utf-8")
    paths = prepare_operation_reference(workspace)
    assert set(paths) == {"operation_guide", "operation_schemas", "operation_examples"}
    scoped = ScopedFiles(workspace)
    guide = scoped.read(paths["operation_guide"], limit=16384)["text"]
    assert 'taskhub_call(operation="inspect"' in guide
    assert '"include_completion": true' in guide
    assert "completion.acceptance_conditions" in guide
    assert "schemas/complete.json" in guide
    assert "schemas/completion_finalize.json" in guide
    for name in OPERATION_SCHEMAS:
        path = f"{paths['operation_schemas']}/{name}.json"
        assert json.loads(scoped.read(path, limit=16384)["text"]) == operation_schema(name)
    for name, example in operation_examples().items():
        path = f"{paths['operation_examples']}/{name}.json"
        assert json.loads(scoped.read(path, limit=16384)["text"]) == example
    before = {path: path.stat().st_mtime_ns for path in workspace.rglob("*") if path.is_file()}
    assert prepare_operation_reference(workspace) == paths
    assert {path: path.stat().st_mtime_ns for path in before} == before
    assert notes.read_text(encoding="utf-8") == "Private source note.\n"
    blobs = BlobStore(tmp_path / "blobs")
    assert [item["path"] for item in blobs.get_json(blobs.snapshot(workspace))["files"]] == ["public.md"]


def test_operation_refresh_repairs_only_generated_files(tmp_path):
    paths = prepare_operation_reference(tmp_path)
    schema = tmp_path / paths["operation_schemas"] / "complete.json"
    schema.write_text("{}\n", encoding="utf-8")
    personal = tmp_path / OPERATION_REFERENCE_DIRECTORY / "personal.md"
    personal.write_text("A user-owned file.\n", encoding="utf-8")
    assert prepare_operation_reference(tmp_path) == paths
    assert json.loads(schema.read_text(encoding="utf-8")) == operation_schema("complete")
    assert personal.read_text(encoding="utf-8") == "A user-owned file.\n"


@pytest.mark.parametrize("relative", [".", ".private", OPERATION_REFERENCE_DIRECTORY])
def test_operation_reference_cannot_follow_a_directory_link(tmp_path, relative):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "README.md"
    sentinel.write_text("Preserve outside data.\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    linked = workspace if relative == "." else workspace / relative
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("The host does not allow directory symlinks")
    with pytest.raises(ValueError, match="links"):
        prepare_operation_reference(workspace)
    assert sentinel.read_text(encoding="utf-8") == "Preserve outside data.\n"
    assert list(outside.iterdir()) == [sentinel]
