from __future__ import annotations

import asyncio
from pathlib import Path

from formal_geyser.coordination.models import TaskContract, ValidationReport

from .lean_service import client as lean_service
from .mathematical_artifacts import snapshot_module_order, validate_snapshot_sources
from .paths import content_hash, module_path
from .registry import (
    ObjectRegistry,
    RegistryError,
)
from .schemas import BuildCandidateRecord, LeanTaskPayload
from .tooling import (
    CHECK_ERRORS,
    error_report,
    require_plain_sources,
    service_failure,
)

BUILD_POLICY = "lean.build@1"


class BuildVerifier:
    """Validate only the narrow claim that frozen source modules compile in a pinned environment.

    `lane` is the Lean service lane: the Integrator's final tree goes first, beside running work.
    """

    def __init__(self, *, lane: str = "official"):
        self.lane = lane

    async def validate(self, hub, task: dict, artifact: dict) -> ValidationReport:
        try:
            return await self._validate(hub, task, artifact)
        except CHECK_ERRORS as error:
            return error_report(error, BUILD_POLICY)

    async def _validate(self, hub, task: dict, artifact: dict) -> ValidationReport:
        contract = TaskContract.model_validate(task["contract"])
        payload = LeanTaskPayload.model_validate(contract.payload)
        if contract.domain != "lean" or contract.acceptance_policy != BUILD_POLICY:
            raise RegistryError("Build validation requires the exact lean.build@1 policy")
        if payload.environment_artifact is None or not payload.modules or len(set(payload.modules)) != len(payload.modules):
            raise RegistryError("A build contract requires an environment and a unique explicit module order")
        if payload.environment_artifact not in contract.inputs:
            raise RegistryError("The build environment must be a frozen task input")
        candidate = BuildCandidateRecord.model_validate(artifact["data"])
        if artifact["kind"] != "lean.build-candidate":
            raise RegistryError("Expected a build-candidate artifact")
        if candidate.task_id != task["id"] or candidate.task_revision != task["revision"]:
            raise RegistryError("Build candidate targets a different task or revision")
        if artifact.get("task_id") != task["id"] or artifact.get("task_revision", artifact.get("revision")) != task["revision"]:
            raise RegistryError("Core build artifact is not attached to the exact task revision")
        snapshot = content_hash(artifact["snapshot"])
        if candidate.snapshot is not None and candidate.snapshot != snapshot:
            raise RegistryError("Build candidate differs from the authoritative core snapshot")
        if candidate.environment_artifact != payload.environment_artifact or candidate.modules != payload.modules:
            raise RegistryError("Build candidate changed the environment or module order")
        if payload.environment_artifact not in artifact.get("input_refs", []):
            raise RegistryError("Build artifact omits its environment dependency edge")
        environment, _ = await ObjectRegistry(hub).get(payload.environment_artifact, "environment", approved=True)
        details = {"validation_method": "lake-build",
            "acceptance_scope": "source-compilation-only", "proof_accepted": False,
            "mathematical_fidelity_reviewed": False, "axiom_policy_checked": False,
            "candidate_artifact": artifact["id"], "snapshot": snapshot,
            "environment_artifact": payload.environment_artifact, "environment_version": environment.version,
            "modules": payload.modules,
        }
        if environment.local_execution != "trusted-code-only":
            return ValidationReport(verdict="unavailable", policy=BUILD_POLICY,
                                    details={**details, "reason": "Local Lean execution is disabled"})
        entries = await asyncio.to_thread(validate_snapshot_sources, hub.blobs, snapshot,
                                          max_files=environment.limits.snapshot_file_limit,
                                          max_bytes=environment.limits.snapshot_bytes)
        all_modules = await asyncio.to_thread(snapshot_module_order, hub.blobs, entries)
        paths = {item["path"] for item in entries}
        for module in payload.modules:
            if module_path(module) not in paths:
                raise RegistryError(f"Build snapshot is missing module {module}")
        require_plain_sources(paths, "Build")
        checked = await lean_service.check(
            Path(hub.root), environment, hub.blobs, snapshot=snapshot, modules=all_modules, lane=self.lane,
            owner="integrator" if self.lane == "integrator" else artifact.get("producer") or "validation",
            label=("Integrated Library " if self.lane == "integrator" else "Build candidate ") + artifact["id"])
        failed = service_failure(checked, details, BUILD_POLICY)
        if failed is not None:
            return failed
        await ObjectRegistry(hub).get(payload.environment_artifact, "environment", approved=True)
        details["build_succeeded"] = True
        details["checked_modules"] = all_modules
        details["trust_assumptions"] = [
            "The operator-authorized local compiler and admitted source are trusted for this development build.",
            "Lake compilation does not establish correspondence with the cited mathematics.",
        ]
        return ValidationReport(verdict="accepted", policy=BUILD_POLICY, details=details)
