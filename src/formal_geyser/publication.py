from __future__ import annotations

import asyncio
import re
from pathlib import Path

from formal_geyser.coordination.models import TaskContract, ValidationReport

from .lean_service import client as lean_service
from .mathematical_artifacts import snapshot_module_order, validate_math_record_snapshot
from .paths import relative_path
from .registry import (
    ObjectRegistry,
    RegistryError,
    reference_ids,
)
from .schemas import (
    METADATA_POLICIES,
    OBJECT_ADAPTER,
    DomainObject,
    MathematicalRecord,
    SemanticReviewEvidence,
    SourceMathematicalRecord,
    StatementRecord,
    stored_record,
)
from .tooling import (
    CHECK_ERRORS,
    error_report,
    service_failure,
)

PEER_POLICY = "lean.peer@1"


def validate_semantic_review(data: dict, report: dict) -> None:
    """Require review findings bound to the actual pair and exact cited sources."""
    if report.get("verdict") != "approve":
        return
    record = stored_record(data)
    if not isinstance(record, MathematicalRecord):
        return
    evidence = SemanticReviewEvidence.model_validate(report.get("semantic_checks"))
    for finding in [*evidence.lean_markdown, *evidence.source_checks]:
        for value, path in ((finding.lean_location, record.bundle.lean_path),
                            (finding.markdown_location, record.bundle.markdown_path)):
            if re.fullmatch(re.escape(path) + r":[1-9][0-9]*(?:-[1-9][0-9]*)?", value) is None:
                raise RegistryError(
                    f"Semantic review locations are {record.bundle.lean_path}:<line> and "
                    f"{record.bundle.markdown_path}:<line> (or <start>-<end>), not {value[:120]!r}: name any "
                    "other file or imported module in the finding text, and give each further range its own finding")
        if not finding.finding.strip():
            raise RegistryError("Semantic correspondence requires concrete findings")
    if isinstance(record, SourceMathematicalRecord):
        required = {(source.literature_id, source.content_sha256, source.locator) for source in record.sources}
        observed = {(source.literature_id, source.content_sha256, source.locator) for source in evidence.source_checks}
        if required != observed:
            raise RegistryError("Definition/statement review must compare every exact cited original source with Lean and Markdown")
        if not record.sources and not evidence.provenance_rationale.strip():
            raise RegistryError("Source-free mathematics requires a reviewed provenance rationale")


def validate_note_publication_paths(paths: list[str], candidate: dict[str, str],
                                    baseline: dict[str, str]) -> None:
    """Notes may select ordinary files and unchanged Library inputs, not change mathematics.

    Both maps bind repository-relative paths to immutable byte hashes. Callers
    must supply the exact recorded publication base, never a worker-supplied
    approximation of its tree. Schema-validated mathematical candidates use
    their existing domain review gates instead of this note-only boundary.
    """
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise RegistryError("Note publication paths must be an explicit list")
    lean_stems = {path[:-5].upper().casefold() for path in {*candidate, *baseline}
                  if path.lower().endswith(".lean")}
    for path in paths:
        relative_path(path)
        associated_markdown = (path.lower().endswith(".md")
                               and path[:-3].upper().casefold() in lean_stems)
        if ((path.lower().endswith(".lean") or associated_markdown)
                and candidate.get(path) != baseline.get(path)):
            raise RegistryError(
                "Notes cannot publish changed Lean or its associated Markdown; "
                "submit a paired lean.definition, lean.statement, or lean.proof artifact")


def publication_policy(contract: TaskContract, record: DomainObject) -> str:
    """Allow only exact metadata policies or the task's original machine policy."""
    if record.object_type in METADATA_POLICIES:
        return METADATA_POLICIES[record.object_type]
    if record.object_type == "environment":
        raise RegistryError("Environment execution requires operator authority")
    expected = {
        "proof": {"lean.proof@1", "lean.development@1"},
        "build-candidate": {"lean.build@1"},
    }.get(record.object_type)
    if expected is None:
        raise RegistryError("This record type has no active publication policy")
    if contract.acceptance_policy not in expected:
        if record.object_type == "proof":
            # In the Linux pilot two executors of a lean.peer@1 program stopped here and asked the
            # operator, reading a separate proof task as duplicate work.
            raise RegistryError(
                f"This task's policy {contract.acceptance_policy} takes no proof candidates. Publish a focused proof "
                "task (acceptance_policy lean.proof@1, naming the target statement) and stage the proof there; "
                "that is the intended route, not duplicate work")
        raise RegistryError("A machine-checked candidate requires its original task policy")
    return contract.acceptance_policy


def validate_publication(contract: TaskContract, kind: str, data: dict, policy: str) -> None:
    if contract.domain != "lean":
        raise RegistryError("Lean publication requires a Lean task")
    if not isinstance(kind, str) or not isinstance(data, dict):
        raise RegistryError("Publication requires an artifact kind and JSON object data")
    if policy == PEER_POLICY:
        if kind.startswith("lean.") or "object_type" in data:
            raise RegistryError("Generic work-product review cannot validate a reserved Lean record")
        return
    record = OBJECT_ADAPTER.validate_python(data)
    if kind != f"lean.{record.object_type}":
        raise RegistryError("Artifact kind and object schema disagree")
    if policy != publication_policy(contract, record):
        raise RegistryError("The publication policy does not match this domain record")


async def validate_metadata(hub, task: dict, artifact: dict) -> ValidationReport:
    policy = task["contract"]["acceptance_policy"]
    details = {"validation_method": "lake-build", "candidate_artifact": artifact["id"],
               "acceptance_scope": "peer-semantic-source-review",
               "peer_review_required": True, "proof_accepted": False,
               "environment_authorization": False, "subject_approval": False}
    try:
        record = stored_record(artifact["data"])
        if record.qualifications:
            # Author scope notes belong to independent semantic review. Keep them
            # distinct from unresolved findings produced by machine validation.
            details["author_qualifications"] = list(record.qualifications)
        if METADATA_POLICIES.get(record.object_type) != policy:
            raise RegistryError("The metadata policy cannot validate this object type")
        if artifact["kind"] != f"lean.{record.object_type}":
            raise RegistryError("Artifact kind and object schema disagree")
        if set(reference_ids(record)) - set(artifact.get("input_refs", [])):
            raise RegistryError("Domain record omits dependency references from core input_refs")
        await ObjectRegistry(hub).validate_for_publication(record)
        details["checks"] = ["schema", "immutable-content", "reference-integrity", "exact-reference-policies"]
        if isinstance(record, MathematicalRecord):
            environment, _ = await ObjectRegistry(hub).get(record.environment_artifact, "environment", approved=True)
            details["bundle"] = record.bundle.model_dump(mode="json")
            details["correspondence_requirements"] = ["lean-markdown"]
            if getattr(record, "sources", []):
                details["correspondence_requirements"].append("source-lean-markdown")
            details["source_provenance"] = getattr(record, "provenance", "internal")
            if environment.local_execution != "trusted-code-only":
                return ValidationReport(verdict="unavailable", policy=policy, details=details,
                                        qualifications=["Mathematical publication requires an authorized Lake build and declaration checks."])
            entries = await asyncio.to_thread(validate_math_record_snapshot, hub.blobs, record, artifact.get("snapshot"),
                max_files=environment.limits.snapshot_file_limit, max_bytes=environment.limits.snapshot_bytes)
            modules = await asyncio.to_thread(snapshot_module_order, hub.blobs, entries)
            # The Lean service builds the frozen snapshot, replays it through the kernel and
            # inspects the declaration, and checks that this left its sources unchanged.
            result = await lean_service.check(
                Path(hub.root), environment, hub.blobs, snapshot=artifact.get("snapshot") or record.bundle.snapshot,
                modules=modules,
                inspections=[{"module": record.entry_module, "declaration": record.declaration}], lane="official",
                owner=artifact.get("producer") or "validation", label="Candidate " + artifact["id"])
            failed = service_failure(result, details, policy, "inspection")
            if failed is not None:
                return failed
            metadata = details["inspection"]["metadata"]
            if metadata["kind"] not in {"definition", "inductive", "constructor"}:
                raise RegistryError("A definition artifact must contain a Lean definition or defined type")
            if isinstance(record, StatementRecord) and (
                    metadata["kind"] != "definition" or not metadata["statement_returns_prop"]):
                raise RegistryError("A statement must be a def/abbrev returning Prop")
            details["checked_modules"] = modules
            details["checks"].extend(["lake-build", "declaration-kind"])
        return ValidationReport(verdict="accepted", policy=policy, details=details)
    except CHECK_ERRORS as error:
        return error_report(error, policy, details)
