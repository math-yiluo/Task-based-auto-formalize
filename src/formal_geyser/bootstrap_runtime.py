"""Cheap, fail-closed admission checks for an initialized Lean project.

Preparation proves the pinned cache with a real import. Admission checks that
receipt and its presence markers, not every byte of the Mathlib build tree.
Compilation and publication retain their independent full validation gates.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from time import monotonic
from typing import Any

from .bootstrap_pins import LEAN_VERSION, pinned_versions
from .coordination.errors import HubError
from .coordination.json_io import read_json
from .coordination.storage import canonical
from .schemas import EnvironmentRecord

NOT_READY = "LEAN_INITIALIZATION_REQUIRED"
# Prepared files change only through explicit preparation, which rewrites the
# initialization record. Their checks repeat when that record or the approved
# environment changes, and at least this often; the ledger is checked every time.
PREPARED_FILES_SECONDS = 60.0


def _failure(reason: str) -> HubError:
    return HubError(NOT_READY, "Lean initialization is not ready: " + reason)


def _absolute(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError(label + " must be an absolute path")
    return Path(value).resolve()


def _owned_cache(hub: Any, cache: dict) -> None:
    project = hub.root.resolve()
    dependencies = (project / ".dependencies").resolve()
    directory = _absolute(cache.get("cache_directory"), "cache_directory")
    if not dependencies.is_relative_to(project) or not directory.is_relative_to(dependencies):
        raise ValueError("the Mathlib cache must belong to this project's .dependencies directory")
    for label in ("receipt_path", "mathlib_directory"):
        if not _absolute(cache.get(label), label).is_relative_to(directory):
            raise ValueError(label + " is outside the project's prepared cache")
    for label in ("library_roots", "source_roots"):
        roots = cache.get(label)
        if not isinstance(roots, list) or not roots:
            raise ValueError(label + " must list the prepared Mathlib paths")
        if any(not _absolute(root, label).is_relative_to(directory) for root in roots):
            raise ValueError(label + " must stay inside the project's prepared cache")


def _prepared_identity(record: dict, cache: dict) -> tuple | None:
    """The initialization record plus the size, time and ID of the presence markers.

    Removing or replacing the ready receipt, owner marker, a compiled-library sentinel or
    an executable changes this immediately; None (never reused) when a marker is missing
    or changed too recently for its metadata to identify it.
    """
    try:
        markers = [cache["receipt_path"], str(Path(cache["cache_directory"]) / "owner.json"),
                   cache["lean_executable"], cache["lake_executable"],
                   *(item["path"] for item in cache["sentinels"])]
        identities = []
        for marker in markers:
            info = os.stat(Path(marker))
            if time.time_ns() - info.st_mtime_ns < 2_000_000_000:
                return None
            identities.append((marker, info.st_size, info.st_mtime_ns, info.st_ino))
    except (KeyError, TypeError, OSError):
        return None
    return (canonical(record), tuple(identities))


def _fresh(hub: Any, part: str, identity: Any) -> bool:
    checked = getattr(hub, "_prepared_checks", {}).get(part)
    return (identity is not None and checked is not None and checked[0] == identity
            and monotonic() - checked[1] < PREPARED_FILES_SECONDS)


def _checked(hub: Any, part: str, identity: Any) -> None:
    if identity is None:
        return
    checks = getattr(hub, "_prepared_checks", None)
    if checks is None:
        checks = hub._prepared_checks = {}
    checks[part] = (identity, monotonic())


def _executable_digest(hub: Any, executable: Path) -> str:
    from .registry import file_sha256

    stat = executable.stat()
    identity = (str(executable), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    previous = getattr(hub, "_bootstrap_executable_digest", None)
    if previous is None or previous[0] != identity:
        previous = (identity, file_sha256(executable))
        hub._bootstrap_executable_digest = previous
    return previous[1]


def require_ready(hub: Any, c: Any) -> dict[str, Any] | None:
    """Validate current readiness within the same transaction as admission."""
    if hub.config.domain != "lean":
        return None
    try:
        path = hub.state_dir / "initialization.json"
        with path.open("rb") as stream:
            record = read_json(stream.read(1048577))
        if type(record.get("format")) is not int or record["format"] != 1:
            raise ValueError("the initialization record has an unsupported format")
        if record.get("state") != "ready":
            raise ValueError("preparation has not completed successfully")
        if record.get("pins") != pinned_versions():
            raise ValueError("the initialization pins differ from the supported Lean/Mathlib baseline")
        cache = record.get("cache")
        if not isinstance(cache, dict):
            raise ValueError("the ready record has no prepared cache receipt")
        identity = _prepared_identity(record, cache)
        if not _fresh(hub, "cache", identity):
            _owned_cache(hub, cache)
            from .bootstrap_cache import validate_cache

            validate_cache(cache)
            _checked(hub, "cache", identity)
        environment_id = record.get("environment_artifact")
        if not isinstance(environment_id, str) or not environment_id:
            raise ValueError("the ready record has no registered environment")
        artifact = c.execute("SELECT * FROM artifacts WHERE id=?", (environment_id,)).fetchone()
        if artifact is None or artifact["kind"] != "lean.environment" or artifact["status"] != "accepted":
            raise ValueError("the prepared environment is not currently accepted")
        evidence = c.execute("SELECT verdict,qualifications_json FROM evidence WHERE artifact_id=? "
                             "AND policy='lean.environment@1' ORDER BY sequence DESC LIMIT 1",
                             (environment_id,)).fetchone()
        if evidence is None or evidence["verdict"] != "accepted" or json.loads(evidence["qualifications_json"]):
            raise ValueError("the prepared environment lacks an unqualified approval")
        environment_identity = (identity, artifact["id"], artifact["body_sha"])
        if not _fresh(hub, "environment", environment_identity):
            environment = EnvironmentRecord.model_validate(hub._record_body(c, dict(artifact)))
            executable = _absolute(cache.get("lean_executable"), "lean_executable")
            if Path(environment.lean_executable).resolve() != executable:
                raise ValueError("the approved environment uses another Lean executable")
            if _executable_digest(hub, executable) != environment.lean_executable_sha256:
                raise ValueError("the approved Lean executable changed after registration")
            if not re.search(r"\b" + re.escape(LEAN_VERSION) + r"\b", environment.lean_version):
                raise ValueError("the approved environment uses another Lean version")
            if [Path(root.path).resolve() for root in environment.library_roots] != [
                    _absolute(root, "library_root") for root in cache["library_roots"]]:
                raise ValueError("the approved environment uses different Mathlib library roots")
            _checked(hub, "environment", environment_identity)
        if (not isinstance(record.get("initial_snapshot"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["initial_snapshot"])
                or not isinstance(record.get("initial_commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40,64}", record["initial_commit"])):
            raise ValueError("the initial source snapshot and commit are missing")
        if (not hub.config.repository.enabled or not (hub.state_dir / "repository.git").is_dir()
                or not hub._repository_head_recorded(c, record["initial_commit"])):
            raise ValueError("the initial source repository is not recorded")
        return record
    except (OSError, ValueError, TypeError, KeyError, HubError) as error:
        raise _failure(str(error)) from error


def resource_roots(prepared: dict[str, Any] | None) -> dict[str, str]:
    if prepared is None:
        return {}
    cache = prepared["cache"]
    # Lean's own sources, readable; its executables run only in the Lean service.
    toolchain = Path(cache["lean_executable"]).parent.parent
    sources = toolchain / "src" / "lean"
    return {"shared_mathlib": cache["cache_directory"],
            "lean_toolchain": str(sources if sources.is_dir() else toolchain)}


def native_environment(prepared: dict[str, Any]) -> dict[str, Any]:
    cache = prepared["cache"]
    return {"environment_artifact": prepared["environment_artifact"], "pins": prepared["pins"],
            "source_roots": cache["source_roots"],
            "instruction": ("Compile and check with lean_check and this environment_artifact: Lean runs only in "
                            "the project's Lean service, never in your shell. Read and search Mathlib's sources "
                            "under source_roots (also LEAN_SRC_PATH); do not download, copy, or rebuild Mathlib.")}


def bind_contract(hub: Any, c: Any, contract: dict) -> dict:
    """Give new Lean work the prepared environment, without rewriting history."""
    if hub.config.domain != "lean":
        return contract
    marker = hub.state_dir / "initialization.json"
    if not marker.is_file():
        return contract  # Operators may draft tasks before preparing the project.
    try:
        with marker.open("rb") as stream:
            record = read_json(stream.read(1048577))
    except (OSError, ValueError, TypeError):
        return contract
    if record.get("state") != "ready":
        return contract
    try:
        prepared = require_ready(hub, c)
    except HubError as error:
        if error.code != NOT_READY:
            raise
        return contract  # Missing readiness blocks execution, not safety/repair records.
    assert prepared is not None
    payload_value, input_values = contract.get("payload", {}), contract.get("inputs", [])
    if (not isinstance(payload_value, dict) or not isinstance(input_values, list)
            or not all(isinstance(item, str) for item in input_values)):
        return contract  # Preserve the ordinary schema's rejection of malformed inputs.
    payload, inputs = dict(payload_value), list(input_values)
    environment_id = prepared["environment_artifact"]
    if payload.get("environment_artifact") not in {None, environment_id}:
        raise HubError("LEAN_ENVIRONMENT_MISMATCH", "New Lean work must use this project's prepared environment")
    for reference in inputs:
        row = c.execute("SELECT kind FROM artifacts WHERE id=?", (reference,)).fetchone()
        if row and row["kind"] == "lean.environment" and reference != environment_id:
            raise HubError("LEAN_ENVIRONMENT_MISMATCH", "New Lean work cannot bind a different execution environment")
    payload["environment_artifact"] = environment_id
    if environment_id not in inputs:
        inputs.append(environment_id)
    return {**contract, "payload": payload, "inputs": inputs}


def require_contract_environment(hub: Any, c: Any, contract: dict) -> None:
    prepared = hub._execution_environment(c)
    if prepared is None:
        return
    environment_id = prepared["environment_artifact"]
    explicit = contract.get("payload", {}).get("environment_artifact")
    if explicit is not None and explicit != environment_id:
        raise HubError("LEAN_ENVIRONMENT_MISMATCH", "Execution requires this project's prepared Lean environment")
    for reference in contract.get("inputs", []):
        row = c.execute("SELECT kind FROM artifacts WHERE id=?", (reference,)).fetchone()
        if row and row["kind"] == "lean.environment" and reference != environment_id:
            raise HubError("LEAN_ENVIRONMENT_MISMATCH", "Execution inputs bind another Lean environment")
