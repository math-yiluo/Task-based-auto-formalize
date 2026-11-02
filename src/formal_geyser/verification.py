from __future__ import annotations

import asyncio
from pathlib import Path

from formal_geyser.coordination.models import TaskContract, ValidationReport

from .lean_service import client as lean_service
from .mathematical_artifacts import snapshot_module_order, source_closure, validate_snapshot_sources
from .paths import content_hash, module_path
from .registry import (
    ObjectRegistry,
    RegistryError,
    read_snapshot,
)
from .schemas import STORED_RECORD, LeanTaskPayload, ProofRecord
from .tooling import (
    CHECK_ERRORS,
    error_report,
    require_plain_sources,
    service_failure,
)

PROOF_POLICY = "lean.proof@1"
DEVELOPMENT_POLICY = "lean.development@1"


async def candidate_base_files(hub, artifact_id: str) -> dict[str, str]:
    """The Library tree the candidate was developed against."""
    binding = await hub.db.call(lambda c: hub._candidate_repository(c, artifact_id))
    if binding is None:
        raise RegistryError("A proof candidate needs its recorded Library base")
    snapshot = await asyncio.to_thread(hub._repository().snapshot, binding["base_commit"], hub.blobs)
    return {item["path"]: item["sha256"] for item in read_snapshot(hub.blobs, snapshot)}


class ProofVerifier:
    """Build the candidate over its Library base and check the theorem proves the named statement.

    The project's Lean service builds the frozen snapshot, replays it through the kernel and
    inspects the theorem; this checks what it reports against the task and the Library.
    """

    async def validate(self, hub, task: dict, artifact: dict) -> ValidationReport:
        policy = task.get("contract", {}).get("acceptance_policy", PROOF_POLICY)
        try:
            return await self._validate(hub, task, artifact)
        except CHECK_ERRORS as error:
            return error_report(error, policy)

    async def _validate(self, hub, task: dict, artifact: dict) -> ValidationReport:
        contract = TaskContract.model_validate(task["contract"])
        payload = LeanTaskPayload.model_validate(contract.payload)
        policy = contract.acceptance_policy
        if policy not in {PROOF_POLICY, DEVELOPMENT_POLICY}:
            raise RegistryError("Unsupported proof validation policy")
        candidate = ProofRecord.model_validate(artifact["data"], context=STORED_RECORD)
        if artifact["kind"] != "lean.proof":
            raise RegistryError("Expected a paired proof artifact")
        if candidate.task_id != task["id"] or candidate.task_revision != task["revision"]:
            raise RegistryError("Candidate targets a different task or revision")
        if artifact.get("task_id") != task["id"] or artifact.get("task_revision", artifact.get("revision")) != task["revision"]:
            raise RegistryError("Core artifact is not attached to this exact task revision")
        candidate_snapshot = content_hash(artifact["snapshot"])
        if candidate.snapshot is not None and artifact["snapshot"] != candidate.snapshot:
            raise RegistryError("Candidate snapshot differs from the core submission snapshot")
        if payload.environment_artifact is None or payload.environment_artifact not in contract.inputs:
            raise RegistryError("Proof validation requires its environment as an explicit task input")
        if candidate.environment_artifact != payload.environment_artifact:
            raise RegistryError("Candidate changed the task environment")
        registry = ObjectRegistry(hub)
        async with registry.verified_reads():
            await registry.validate_for_publication(candidate)
            environment, _ = await registry.get(payload.environment_artifact, "environment", approved=True)
        limits = environment.limits
        entries = await asyncio.to_thread(validate_snapshot_sources, hub.blobs, candidate_snapshot, bundle=candidate.bundle,
                                          max_files=limits.snapshot_file_limit, max_bytes=limits.snapshot_bytes)
        files = {item["path"]: item["sha256"] for item in entries}
        if module_path(candidate.entry_module) not in files:
            raise RegistryError("Candidate entry module is absent from the submitted snapshot")
        require_plain_sources(files, "Candidate")
        base = await candidate_base_files(hub, artifact["id"])
        details = {
            "validation_method": "lake-build",
            "candidate_artifact": artifact["id"], "candidate_snapshot": candidate_snapshot,
            "environment_artifact": candidate.environment_artifact,
            "environment_version": environment.version,
            "statement_declaration": candidate.statement_declaration,
            "bundle": candidate.bundle.model_dump(mode="json"),
            "correspondence_requirements": ["lean-markdown"],
            "proof_accepted": False,
        }
        if environment.local_execution != "trusted-code-only":
            return ValidationReport(verdict="unavailable", policy=policy, details={**details,
                "reason": "Local Lean execution is disabled"})
        modules = await asyncio.to_thread(snapshot_module_order, hub.blobs, entries)
        # The service builds the frozen snapshot itself and checks that compiling and
        # inspecting it left its sources unchanged.
        result = await lean_service.check(
            Path(hub.root), environment, hub.blobs, snapshot=candidate_snapshot, modules=modules,
            inspections=[{"module": candidate.entry_module, "declaration": candidate.declaration,
                          "statement": candidate.statement_declaration}],
            lane="official", owner=artifact.get("producer") or "validation", label="Proof candidate " + artifact["id"])
        failed = service_failure(result, details, policy, "candidate_inspection")
        if failed is not None:
            return failed
        metadata = details["candidate_inspection"]["metadata"]
        if metadata.get("kind") != "theorem" or not metadata.get("target_matches"):
            raise RegistryError("The candidate theorem does not prove the named statement")
        statement_path = module_path(metadata["statement_module"])
        if statement_path not in base:
            raise RegistryError("A proof must target a statement already in its Library base")
        # The statement, everything it imports, and their Markdown keep their
        # base bytes, so a proof cannot weaken the proposition it claims to prove.
        base_entries = [{"path": path, "sha256": sha} for path, sha in base.items()]
        for item in source_closure(hub.blobs, base_entries, [statement_path]):
            if files.get(item["path"]) != item["sha256"]:
                raise RegistryError("A proof candidate changed its target statement or its imports: " + item["path"])
        details.update(proof_accepted=policy == PROOF_POLICY, checked_modules=modules,
                       checks=["lake-build", "exact-target", "unchanged-statement", "frozen-inputs"],
                       peer_review_required=True)
        return ValidationReport(verdict="accepted" if policy == PROOF_POLICY else "needs_operator",
            policy=policy, details=details,
            qualifications=[] if policy == PROOF_POLICY else ["Development evidence does not publish a proof."])
