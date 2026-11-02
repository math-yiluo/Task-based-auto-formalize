"""On-demand operation formats shared by every domain, outside fixed prompts."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from ..content import PRIVATE_DIRECTORY
from .files import ScopedFiles

OPERATION_REFERENCE_DIRECTORY = f"{PRIVATE_DIRECTORY}/taskhub-reference/operations"
OPERATION_SCHEMAS = (
    "completion-evidence", "completion-resolution", "complete",
    "completion_finalize",
)


def operation_schema(operation: str) -> dict:
    """Describe authored arguments; exact authority and acceptance remain backend checks."""
    text = {"type": "string", "minLength": 1, "pattern": r"\S"}
    artifacts = {"type": "array", "minItems": 1, "maxItems": 100, "items": text}
    condition = {
        "type": "object", "additionalProperties": False,
        "required": ["explanation", "artifact_ids"],
        "properties": {
            "explanation": {**text, "maxLength": 10000},
            "artifact_ids": artifacts,
        },
    }
    evidence = {
        "type": "object", "minProperties": 1, "propertyNames": text,
        "description": "Map every exact acceptance_conditions key returned by inspect(include_completion=true).completion.",
        "additionalProperties": condition,
    }
    resolution = {
        "type": "object", "propertyNames": text,
        "description": "Map every exact feedback.conditions objection; use {} when there are none.",
        "additionalProperties": {
            "type": "object", "additionalProperties": False,
            "required": ["outcome", "explanation", "artifact_ids"],
            "properties": {
                "outcome": {"enum": ["satisfied", "optional_extension"]},
                "explanation": text, "artifact_ids": artifacts,
            },
        },
    }
    schemas: dict[str, dict] = {
        "completion-evidence": evidence,
        "completion-resolution": resolution,
        "complete": {
            "type": "object", "additionalProperties": False,
            "required": ["task_id", "revision", "evidence", "reason"],
            "properties": {
                "task_id": text, "revision": {"type": "integer", "minimum": 1},
                "evidence": evidence, "reason": text,
            },
        },
        "completion_finalize": {
            "type": "object", "additionalProperties": False,
            "required": ["declaration_id", "decision", "reason"],
            "properties": {
                "declaration_id": text, "decision": {"enum": ["close", "continue"]},
                "reason": {**text, "maxLength": 20000},
                "evidence": evidence, "resolution": resolution,
            },
        },
    }
    if operation not in schemas:
        raise ValueError("Unknown operation reference schema")
    return deepcopy({"$schema": "https://json-schema.org/draft/2020-12/schema",
                     **schemas[operation]})


def operation_examples() -> dict[str, dict]:
    """Format-only examples; their placeholder IDs never constitute acceptance evidence."""
    evidence = {
        "objective": {
            "explanation": "The accepted result fulfills the original objective in full.",
            "artifact_ids": ["accepted-artifact-id"],
        },
        "acceptance:0": {
            "explanation": "The published source and its validation satisfy this exact condition.",
            "artifact_ids": ["accepted-artifact-id"],
        },
    }
    finalize: dict = {
        "declaration_id": "completion-declaration-id", "decision": "close",
        "reason": "All original and current conditions and required publications are satisfied.",
        "evidence": evidence,
        "resolution": {
            "acceptance:0": {
                "outcome": "satisfied",
                "explanation": "The accepted artifact's published result supplies the requested evidence.",
                "artifact_ids": ["accepted-artifact-id"],
            },
        },
    }
    optional = deepcopy(finalize)
    optional["resolution"]["acceptance:0"].update(
        outcome="optional_extension",
        explanation="The accepted result already fulfills this unchanged condition; the proposed additional scope is a separate task.",
    )
    return {
        "complete": {"task_id": "task-id", "revision": 1, "evidence": evidence,
                             "reason": "The exact accepted publications fulfill all inspected conditions."},
        "completion_finalize": finalize,
        "completion_finalize-optional-extension": optional,
    }


def prepare_operation_reference(workspace: Path) -> dict[str, str]:
    """Refresh private native references without changing unrelated workspace files."""
    workspace.mkdir(parents=True, exist_ok=True)
    files = ScopedFiles(workspace)
    references = {
        "README.md": Path(__file__).with_name("operation-guide.md").read_text(encoding="utf-8"),
        **{f"schemas/{name}.json": json.dumps(operation_schema(name), ensure_ascii=False, indent=2) + "\n"
           for name in OPERATION_SCHEMAS},
        **{f"examples/{name}.json": json.dumps(example, ensure_ascii=False, indent=2) + "\n"
           for name, example in operation_examples().items()},
    }
    for relative, text in references.items():
        name = f"{OPERATION_REFERENCE_DIRECTORY}/{relative}"
        target = files.path(name, must_exist=False)
        if target.is_file() and target.read_bytes() == text.encode("utf-8"):
            continue
        files.write(name, text)
    return {
        "operation_guide": f"{OPERATION_REFERENCE_DIRECTORY}/README.md",
        "operation_schemas": f"{OPERATION_REFERENCE_DIRECTORY}/schemas",
        "operation_examples": f"{OPERATION_REFERENCE_DIRECTORY}/examples",
    }
