"""Resolve exact domain bounds before environment-aware source validation."""
from __future__ import annotations

from typing import TypedDict

from .coordination.snapshot_limits import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES
from .registry import ObjectRegistry, RegistryError


class SnapshotBounds(TypedDict):
    max_files: int
    max_bytes: int


async def submission_environment(hub, task: dict, payload: dict, *, required: bool = False) -> str | None:
    """Resolve one exact environment consistently, without granting approval."""
    data = payload.get("data", {})
    contract = task["contract"]
    pinned_environment = contract.get("payload", {}).get("environment_artifact")
    if (pinned_environment is not None and "environment_artifact" in data
            and (type(data["environment_artifact"]) is not type(pinned_environment)
                 or data["environment_artifact"] != pinned_environment)):
        raise RegistryError("Submitted contribution environment_artifact differs from its authoritative value")
    environment_id = pinned_environment if pinned_environment is not None else data.get("environment_artifact")
    if environment_id is None:
        environments = set()
        for reference in contract.get("inputs", []):
            artifact = await hub.artifact(reference)
            if artifact and artifact["kind"] == "lean.environment":
                environments.add(reference)
        if len(environments) > 1:
            raise RegistryError("Submitted Lean record requires an unambiguous environment artifact")
        if len(environments) == 1:
            environment_id = environments.pop()
    if environment_id is not None and (not isinstance(environment_id, str) or not environment_id):
        raise RegistryError("Submitted Lean record requires an immutable environment artifact ID")
    if environment_id is None and required:
        raise RegistryError("Submitted Lean record requires an unambiguous environment artifact")
    return environment_id


async def submission_limits(hub, task: dict, payload: dict) -> SnapshotBounds:
    environment_id = await submission_environment(hub, task, payload)
    if environment_id is None:
        # Metadata and generic notes can precede an environment selection. This
        # is the shared bounded envelope, never a smaller substitute environment.
        return {"max_files": MAX_SNAPSHOT_FILES, "max_bytes": MAX_SNAPSHOT_BYTES}
    environment, _ = await ObjectRegistry(hub).get(environment_id, "environment")
    return {"max_files": environment.limits.snapshot_file_limit, "max_bytes": environment.limits.snapshot_bytes}
