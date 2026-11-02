"""Normalize native submission records without granting publication authority."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from formal_geyser.coordination.content import PRIVATE_DIRECTORY
from formal_geyser.coordination.models import TaskContract

from .paths import module_path, relative_path
from .publication import PEER_POLICY, publication_policy, validate_publication
from .registry import ObjectRegistry, RegistryError, read_snapshot, reference_ids
from .schemas import (
    OBJECT_ADAPTER,
    BuildCandidateRecord,
    LeanTaskPayload,
    MathematicalRecord,
)
from .snapshot_policy import submission_environment, submission_limits

_MATHEMATICAL_TYPES = {"definition", "statement", "proof"}
_RECEIPT_FLAGS = {
    "accepted", "proof_accepted", "compilation_verified", "environment_authorization",
    "subject_approval", "publication_accepted",
}


def _exact_or_fill(data: dict, field: str, expected: Any) -> None:
    """Supply an unambiguous value, but never repair an author's conflicting claim."""
    if field in data and (type(data[field]) is not type(expected) or data[field] != expected):
        raise RegistryError(f"Submitted contribution {field} differs from its authoritative value")
    data[field] = deepcopy(expected)


def _paths(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
        raise RegistryError("Submitted repository_paths must be a list of relative paths")
    result = [relative_path(path) for path in value]
    if len({path.casefold() for path in result}) != len(result):
        raise RegistryError("Submitted repository_paths contain duplicate or colliding paths")
    if any(part.upper().casefold() in {".task-based", ".taskhub-contributions", PRIVATE_DIRECTORY}
           for path in result for part in path.split("/")):
        raise RegistryError("Private notes and contribution control files cannot be published as source")
    return result


def _input_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(ref, str) or not ref for ref in value):
        raise RegistryError("Submitted input_refs must be a list of immutable artifact IDs")
    if len(set(value)) != len(value):
        raise RegistryError("Submitted input_refs contain duplicate artifact IDs")
    return value


def _reject_receipt_claims(data: Any) -> None:
    """Test observations are allowed; authors cannot issue trusted receipts."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _RECEIPT_FLAGS and value not in (False, None):
                raise RegistryError("Submitted work products cannot claim acceptance or verifier authority")
            if key == "verdict" and value in ("accepted", "verified"):
                raise RegistryError("Submitted work products cannot issue acceptance or verifier receipts")
            _reject_receipt_claims(value)
    elif isinstance(data, list):
        for value in data:
            _reject_receipt_claims(value)


async def _environment(registry: ObjectRegistry, contract: TaskContract,
                       data: dict, *, versioned: bool) -> None:
    environment_id = await submission_environment(registry.hub,
        {"contract": contract.model_dump(mode="json")}, {"data": data}, required=True)
    _exact_or_fill(data, "environment_artifact", environment_id)
    environment, _ = await registry.get(data["environment_artifact"], "environment")
    if versioned:
        _exact_or_fill(data, "environment_version", environment.version)


def _bundle(data: dict, files: dict[str, str], snapshot_sha: str) -> None:
    entry = data.get("entry_module")
    if not isinstance(entry, str):
        raise RegistryError("Submitted mathematics requires an explicit entry_module")
    lean_path = module_path(entry)
    markdown_path = lean_path[:-5] + ".md"
    value = data.setdefault("bundle", {})
    if not isinstance(value, dict):
        raise RegistryError("Submitted mathematical bundle must be a JSON object")
    _exact_or_fill(value, "lean_path", lean_path)
    _exact_or_fill(value, "markdown_path", markdown_path)
    for path in (lean_path, markdown_path):
        if path not in files:
            raise RegistryError(f"Submitted mathematics requires same-stem Lean/Markdown files: {path}")
    _exact_or_fill(value, "snapshot", snapshot_sha)
    _exact_or_fill(value, "lean_sha256", files[lean_path])
    _exact_or_fill(value, "markdown_sha256", files[markdown_path])


async def normalize_submission_record(hub, task: dict, payload: dict,
                                          snapshot_sha: str) -> dict:
    """Fill native manifest fields from exact task, environment, and source bindings.

    Only pinned task/registry values and frozen workspace bytes supply defaults.
    No compiler, model, worker execution, approval, or mutable registry write is used.
    """
    from .plugin import LeanPlugin

    contract = TaskContract.model_validate(task["contract"])
    plugin = LeanPlugin()
    plugin.validate_contract(contract)
    result = deepcopy(payload)
    for field, expected in (("task_id", task["id"]), ("task_revision", task["revision"])):
        _exact_or_fill(result, field, expected)
    kind, data = result.get("kind"), result.get("data")
    if not isinstance(kind, str) or not kind or not isinstance(data, dict):
        raise RegistryError("Submitted publication requires a record kind and JSON object data")
    paths = _paths(result.get("repository_paths", []))
    refs = _input_refs(result.get("input_refs", []))
    limits = await submission_limits(hub, task, payload)
    files = {item["path"]: item["sha256"] for item in read_snapshot(hub.blobs, snapshot_sha, **limits)}
    if any(part.upper().casefold() in {".task-based", ".taskhub-contributions", PRIVATE_DIRECTORY}
           for path in files for part in path.split("/")):
        raise RegistryError("Source snapshots must exclude contribution control files and private notes")
    task_payload = LeanTaskPayload.model_validate(contract.payload)
    required_paths: list[str] = []
    record_refs: list[str] = []
    if kind.startswith("lean.") or "object_type" in data:
        if not kind.startswith("lean."):
            raise RegistryError("Artifact kind and reserved Lean record disagree")
        object_type = kind.removeprefix("lean.")
        _exact_or_fill(data, "object_type", object_type)
        registry = ObjectRegistry(hub)
        if object_type in _MATHEMATICAL_TYPES or object_type == "build-candidate":
            await _environment(registry, contract, data,
                               versioned=object_type in _MATHEMATICAL_TYPES)
        if object_type in {"proof", "build-candidate"}:
            for field, expected in (("task_id", task["id"]), ("task_revision", task["revision"]),
                                    ("snapshot", snapshot_sha)):
                _exact_or_fill(data, field, expected)
        if object_type == "build-candidate":
            _exact_or_fill(data, "modules", task_payload.modules)
        if object_type in _MATHEMATICAL_TYPES:
            _bundle(data, files, snapshot_sha)
        record = OBJECT_ADAPTER.validate_python(data)
        policy = publication_policy(contract, record)
        # This is the same immutable-reference gate used by native worker staging.
        # Required approval receipts and machine verification remain review gates.
        await registry._validate_references(record)
        record_refs = reference_ids(record)
        data = record.model_dump(mode="json")
        validation_paths = []
        if isinstance(record, MathematicalRecord):
            required_paths = [record.bundle.lean_path, record.bundle.markdown_path]
            validation_paths = required_paths
        elif isinstance(record, BuildCandidateRecord):
            validation_paths = [path for module in record.modules for path in
                                (module_path(module), module_path(module)[:-5] + ".md")]
        if any(path not in files for path in validation_paths):
            raise RegistryError("Submitted contribution omits a required Lean/Markdown publication file")
    else:
        _reject_receipt_claims(data)
        policy = PEER_POLICY
    _exact_or_fill(result, "policy", policy)
    result["data"] = data
    result["input_refs"] = sorted(set(refs) | set(contract.inputs) | set(record_refs))
    result["repository_paths"] = _paths(list(dict.fromkeys([*paths, *required_paths])))
    validate_publication(contract, kind, data, policy)
    await asyncio.to_thread(plugin.validate_candidate_snapshot, hub, task, result, snapshot_sha, limits=limits)
    # The core staging boundary validates selected paths against the worker
    # workspace base. A manifest cannot supply its own integration base.
    return result
