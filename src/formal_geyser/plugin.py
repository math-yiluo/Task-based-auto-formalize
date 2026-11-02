from __future__ import annotations

import asyncio
from pathlib import Path

from formal_geyser.coordination.models import TaskContract, ValidationReport

from .build_verification import BUILD_POLICY, BuildVerifier
from .mathematical_artifacts import snapshot_module_order, validate_snapshot_sources
from .publication import (
    PEER_POLICY,
    validate_metadata,
    validate_note_publication_paths,
    validate_publication,
)
from .registry import RegistryError, read_snapshot
from .schemas import (
    METADATA_POLICIES,
    OBJECT_ADAPTER,
    STORED_RECORD,
    BuildCandidateRecord,
    LeanTaskPayload,
    MathematicalRecord,
    ProofRecord,
)
from .snapshot_policy import submission_environment, submission_limits
from .verification import DEVELOPMENT_POLICY, PROOF_POLICY, ProofVerifier


def _storage_failure(error: OSError) -> str:
    """Why a check could not read its stored input. Validation reads a candidate only from the
    content store, never from paths the candidate names, so an operating-system error there is a
    failing disk or store (in the Linux pilot a full disk failed every read), not a defect of the
    candidate: the check is unavailable and runs again, instead of sending correct work back."""
    return f"The stored input could not be read: {type(error).__name__}: {error}"


class LeanPlugin:
    # Lean runs in the project's Lean service, which keeps an official check going when TaskHub
    # stops waiting for it (a pause, a restart): asking again attaches to the same check.
    validations_survive_interruption = True

    def submission_wait(self, hub):
        """Inside a worker's activation a validation waits this long, then continues in the
        background and its result arrives as a notification."""
        from .lean_service import client
        return client.waiting(hub.config.lean_service.validation_wait_seconds)

    async def resource_status(self, hub) -> dict | None:
        """The Lean service's jobs and the machine, for the dashboard; None when it is not running."""
        from .lean_service import client
        return await client.current_status(Path(hub.root))

    def validate_review_report(self, artifact: dict, report: dict) -> None:
        from .publication import validate_semantic_review

        if artifact["kind"] in {"lean.definition", "lean.statement", "lean.proof"}:
            validate_semantic_review(artifact["data"], report)

    name = "lean"

    def default_codex_home(self) -> str:
        return str(Path.home() / ".local" / "share" / "formal-geyser" / "codex")

    def default_claude_home(self) -> str:
        # The CLAUDE_CONFIG_DIR that holds this machine's Claude subscription sign-in.
        return str(Path.home() / ".local" / "share" / "formal-geyser" / "claude")

    def worker_tools(self) -> list[dict]:
        from .worker_tools import worker_tools
        return worker_tools()

    def worker_instructions(self) -> str:
        from .worker_tools import WORKER_INSTRUCTIONS
        return WORKER_INSTRUCTIONS

    def worker_guides(self, *, review: bool = False) -> dict[str, str]:
        directory = Path(__file__).with_name("guides")
        # A reviewer reads its own Lean note; lean-operations.md says how to run a lean_check probe.
        names = (("lean-review.md", "lean-operations.md", "lean-review-operations.md")
                 if review else ("lean.md", "lean-operations.md"))
        return {name: (directory / name).read_text(encoding="utf-8") for name in names}

    def prepare_worker_reference(self, workspace: Path) -> dict[str, str]:
        from .submission_reference import prepare_reference
        return prepare_reference(workspace)

    async def handle_worker_tool(self, hub, name: str, arguments: dict, actor, *, key: str) -> dict:
        from .worker_tools import handle_worker_tool
        return await handle_worker_tool(hub, name, arguments, actor, key=key)

    def validate_contract(self, contract: TaskContract) -> None:
        if contract.domain != self.name:
            raise ValueError("Lean plugin requires domain='lean'")
        if contract.acceptance_policy not in {PROOF_POLICY, DEVELOPMENT_POLICY, BUILD_POLICY, PEER_POLICY,
                                               *METADATA_POLICIES.values()}:
            raise ValueError("Unknown Lean acceptance policy")
        payload = LeanTaskPayload.model_validate(contract.payload)
        if contract.acceptance_policy == BUILD_POLICY:
            if not payload.environment_artifact or not payload.modules:
                raise ValueError("Build tasks require an environment and an explicit module order")
            if len(set(payload.modules)) != len(payload.modules):
                raise ValueError("Build task modules must be unique")
            if payload.environment_artifact not in contract.inputs:
                raise ValueError("The build environment must be an explicit immutable input")
        if contract.acceptance_policy in {PROOF_POLICY, DEVELOPMENT_POLICY}:
            if payload.environment_artifact is None or payload.environment_artifact not in contract.inputs:
                raise ValueError("Proof tasks require their environment as an explicit task input")

    def validate_publication(self, contract: TaskContract, kind: str, data: dict, policy: str) -> None:
        validate_publication(contract, kind, data, policy)

    async def normalize_submission(self, hub, task: dict, payload: dict, snapshot_sha: str) -> dict:
        """Normalize native files against exact task and domain bindings."""
        from .submission_records import normalize_submission_record
        if not payload["kind"].startswith("lean.") and "object_type" not in payload["data"]:
            await submission_environment(hub, task, payload)
            return payload
        # The core staging boundary compares selected paths against its recorded
        # author base. A native manifest must not supply or invent that base.
        return await normalize_submission_record(hub, task, payload, snapshot_sha)

    def validate_candidate_snapshot(self, hub, task: dict, payload: dict, snapshot_sha: str, *, limits=None,
                                    stored: bool = False) -> None:
        """Fail closed before staging, including generic publication bypass attempts."""
        record = None
        if payload.get("kind", "").startswith("lean.") or "object_type" in payload.get("data", {}):
            record = OBJECT_ADAPTER.validate_python(payload["data"], context=STORED_RECORD if stored else None)
        bundle = record.bundle if isinstance(record, MathematicalRecord) else None
        validate_snapshot_sources(hub.blobs, snapshot_sha, bundle=bundle, **(limits or {}))
        if isinstance(record, MathematicalRecord) and payload.get("kind") != f"lean.{record.object_type}":
            raise RegistryError("Artifact kind and mathematical record disagree")
        if isinstance(record, ProofRecord):
            target = LeanTaskPayload.model_validate(task["contract"]["payload"])
            if ((record.task_id, record.task_revision, record.environment_artifact)
                    != (task["id"], task["revision"], target.environment_artifact)):
                raise RegistryError("A proof candidate must keep its task revision and environment")
        declared_snapshot = getattr(record, "snapshot", None)
        if declared_snapshot is not None and declared_snapshot != snapshot_sha:
            raise RegistryError("Record snapshot differs from the authoritative snapshot")

    def validate_repository_snapshot(self, hub, snapshot_sha: str) -> None:
        """Repository initialization uses the same pairing and hole boundary."""
        validate_snapshot_sources(hub.blobs, snapshot_sha)

    def publication_paths(self, task: dict, payload: dict) -> list[str]:
        if not payload.get("kind", "").startswith("lean."):
            return []
        record = OBJECT_ADAPTER.validate_python(payload["data"])
        if isinstance(record, MathematicalRecord):
            return [record.bundle.lean_path, record.bundle.markdown_path]
        return []

    @staticmethod
    def _mathematical_publication(payload: dict) -> bool:
        kind, data = payload.get("kind", ""), payload.get("data", {})
        if kind.startswith("lean.") or "object_type" in data:
            record = OBJECT_ADAPTER.validate_python(data)
            if kind != f"lean.{record.object_type}":
                raise RegistryError("Publication kind and domain record disagree")
            return isinstance(record, MathematicalRecord)
        return False

    @staticmethod
    def _publication_base_files(hub, base_commit: str | None) -> dict[str, str]:
        repository_factory = getattr(hub, "_repository", None)
        if base_commit is None or not callable(repository_factory):
            # Without a managed repository there is no accepted source tree.
            return {}
        snapshot = repository_factory().snapshot(base_commit, hub.blobs)
        return {item["path"]: item["sha256"] for item in read_snapshot(hub.blobs, snapshot)}

    def validate_publication_paths(self, hub, task: dict, payload: dict, snapshot_sha: str,
                                   base_commit: str | None, paths: list[str]) -> None:
        """Staging supplies its recorded author base and combined selected paths."""
        if self._mathematical_publication(payload) or not paths:
            return
        candidate = {item["path"]: item["sha256"] for item in read_snapshot(hub.blobs, snapshot_sha)}
        baseline = self._publication_base_files(hub, base_commit)
        validate_note_publication_paths(paths, candidate, baseline)

    async def validate_final_tree(self, hub, items: list[dict], snapshot_sha: str) -> ValidationReport:
        """One source scan and Lake build of the complete final Library tree.

        Contributions keep no per-item identity in the Library: the integrated
        tree is valid when it contains no holes or new axioms and builds.
        """
        details = {"integration_snapshot": snapshot_sha, "fresh_integrated_validation": True,
                   "proof_accepted": False}
        try:
            first = items[0]
            limits = await submission_limits(hub, first["task"], first["artifact"])
            entries = await asyncio.to_thread(validate_snapshot_sources, hub.blobs, snapshot_sha, **limits)
            modules = await asyncio.to_thread(snapshot_module_order, hub.blobs, entries)
            if not modules:
                return ValidationReport(verdict="accepted", policy=BUILD_POLICY,
                                        details={**details, "checked_modules": []})
            environment_id = None
            for item in items:
                environment_id = await submission_environment(hub, item["task"], item["artifact"])
                if environment_id:
                    break
            if not environment_id:
                return ValidationReport(verdict="unavailable", policy=BUILD_POLICY, details={
                    **details, "reason": "The final Lean tree requires an authorized environment for a Lake build"})
            contract = TaskContract(
                title="Validate the final integrated Library", objective="Build every final Lean module.",
                domain="lean", acceptance_policy=BUILD_POLICY, inputs=[environment_id],
                payload={"environment_artifact": environment_id, "modules": modules})
            record = BuildCandidateRecord(
                name="Final integrated Library", environment_artifact=environment_id,
                task_id=first["task"]["id"], task_revision=first["task"]["revision"],
                snapshot=snapshot_sha, modules=modules)
            artifact = {**first["artifact"], "kind": "lean.build-candidate", "snapshot": snapshot_sha,
                        "data": record.model_dump(mode="json"), "input_refs": [environment_id]}
            # The Integrator's build goes first in the Lean service, beside work already running.
            report = await BuildVerifier(lane="integrator").validate(
                hub, {**first["task"], "contract": contract.model_dump(mode="json")}, artifact)
            report.details.update(details, checked_modules=modules)
            return report
        except OSError as error:
            return ValidationReport(verdict="unavailable", policy=BUILD_POLICY,
                                    details={**details, "reason": _storage_failure(error)})
        except (ValueError, KeyError, TypeError) as error:
            return ValidationReport(verdict="rejected", policy=BUILD_POLICY,
                                    details={**details, "reason": str(error)})

    async def validate_candidate(self, hub, task: dict, artifact: dict) -> ValidationReport:
        contract = TaskContract.model_validate(task["contract"])
        self.validate_contract(contract)
        if artifact.get("snapshot"):
            try:
                limits = await submission_limits(hub, task, artifact)
                await asyncio.to_thread(self.validate_candidate_snapshot, hub, task, artifact, artifact["snapshot"],
                                        limits=limits, stored=True)
            except OSError as error:
                return ValidationReport(verdict="unavailable", policy=contract.acceptance_policy,
                                        details={"reason": _storage_failure(error), "proof_accepted": False})
            except (ValueError, KeyError, TypeError) as error:
                return ValidationReport(verdict="rejected", policy=contract.acceptance_policy,
                                        details={"reason": str(error), "proof_accepted": False})
        if contract.acceptance_policy == BUILD_POLICY:
            return await BuildVerifier().validate(hub, task, artifact)
        if contract.acceptance_policy == PEER_POLICY:
            try:
                self.validate_publication(contract, artifact["kind"], artifact["data"], PEER_POLICY)
            except ValueError as error:
                return ValidationReport(verdict="rejected", policy=PEER_POLICY,
                                        details={"reason": str(error), "proof_accepted": False})
            return ValidationReport(verdict="accepted", policy=PEER_POLICY,
                                    details={"candidate_artifact": artifact["id"],
                                             "acceptance_scope": "peer-reviewed-work-product",
                                             "peer_review_required": True, "proof_accepted": False,
                                             "compilation_verified": False, "environment_authorization": False})
        if contract.acceptance_policy in METADATA_POLICIES.values():
            return await validate_metadata(hub, task, artifact)
        return await ProofVerifier().validate(hub, task, artifact)
