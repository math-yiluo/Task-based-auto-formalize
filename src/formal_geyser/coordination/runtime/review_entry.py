"""Compact review inputs backed by exact, readable, immutable evidence.

This is a runtime view. It neither edits review contracts nor migrates historical
records, and it does not acknowledge any context delivery or event.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..content import _path
from ..errors import HubError
from ..material_files import DeferredMaterial, MaterialFiles
from ..models import Actor, Role
from ..native import MATHEMATICAL_KINDS
from ..storage import canonical, collected_thread_call
from .model_view import tool_view


def _detail(material: MaterialFiles, value: Any) -> Any:
    """Keep short instructions exact; retain every byte of longer instructions."""
    return value if len(canonical(value).encode("utf-8")) <= 1600 else material.json(value)


def _business_details(hub: Any, c: Any, value: Any) -> Any:
    """Name coordinator references and remove execution metadata before writing.

    The shared projection preserves authored records, contracts, and evidence.
    Wrapping permits lists of integration rounds without treating them as an
    arbitrary task packet. Neither the source records nor their hashes change.
    """
    named = hub._present_worker_data(c, value)
    return tool_view({"value": named})["value"]


def _readable_material(material: MaterialFiles, body: bytes) -> dict:
    reference = material.write(body)
    return {"path": str(material.workspace / reference["material_path"]),
            "sha256": reference["material_sha256"], **reference}


def _task_details(task: dict) -> dict:
    """Retain task authority without making ordinary attempt changes new evidence."""
    result = {key: task[key] for key in (
        "id", "revision", "current_revision", "contract_hash", "state", "contract", "files")}
    scope = task["collaboration"]
    result["scope"] = {key: scope[key] for key in (
        "mode", "host_task_id", "governance", "publishers", "created_by", "created_at") if key in scope}
    return result


def host_entry(task: dict, conditions: dict, material: MaterialFiles) -> dict:
    contract = task["contract"]
    instructions = {key: contract[key] for key in (
        "objective", "constraints", "requirements", "context", "payload")}
    files = task["files"]
    if files is None:
        files = {"task_id": task["id"], "revision": task["revision"],
                 "contract_sha256": task["contract_hash"],
                 **_readable_material(material, canonical(contract).encode("utf-8"))}
    return {"id": task["id"], "revision": task["revision"],
            "current_revision": task["current_revision"], "contract_hash": task["contract_hash"],
            "title": contract["title"], "acceptance_policy": contract["acceptance_policy"],
            "acceptance_instructions": _detail(material, instructions),
            "acceptance_conditions": _detail(material, conditions),
            "files": files}


def _candidate_entry(artifact: dict, material: MaterialFiles) -> dict:
    # _artifact has already run _record_body and _record_files. Never recover a
    # missing or changed native file from an older blob or the author's workspace.
    record = artifact["content_file"]
    if record is None:
        raise HubError("CONTENT_REFERENCE_MISSING", "Review requires the candidate's authoritative native files")
    result = {"id": artifact["id"], "kind": artifact["kind"], "hash": artifact["body_sha"],
              "snapshot": artifact["snapshot"], "record_file": {
                  key: value for key, value in record.items() if key not in {"source_files", "removed_paths"}}}
    if record.get("source_files"):
        result["source_files"] = _detail(material, record["source_files"])
        if record.get("removed_paths"):
            result["removed_paths"] = _detail(material, record["removed_paths"])
    data = artifact["data"]
    bundle = data.get("bundle") if isinstance(data, dict) else None
    if artifact["kind"] in MATHEMATICAL_KINDS and isinstance(bundle, dict):
        pair: dict[str, dict[str, Any]] = {}
        for name in ("lean", "markdown"):
            source_path, sha = bundle.get(name + "_path"), bundle.get(name + "_sha256")
            if not isinstance(source_path, str) or not isinstance(sha, str):
                continue
            path = _path(Path(record["root"]), "files/" + source_path)
            pair[name] = {"path": str(path), "source_path": source_path, "sha256": sha}
        if pair:
            result["primary_pair"] = pair
    return result


def _introduction(review: dict, artifact: dict | None = None) -> str:
    contract = review["contract"]
    value = contract.get("payload", {}).get("introduction") or contract.get("context")
    if not isinstance(value, str) or not value.strip():
        data = artifact["data"] if artifact is not None else {}
        value = next((data[key] for key in ("summary", "name", "description")
                      if isinstance(data, dict) and isinstance(data.get(key), str)
                      and data[key].strip()), None)
    return value or contract["title"]


def _author_introduction(material: MaterialFiles, review: dict, artifact: dict | None = None) -> Any:
    value = _introduction(review, artifact)
    return value if len(value) <= 4000 else material.json(value)


async def prepare_review_entry(hub: Any, attempt: dict) -> dict:
    """Read the active review's exact basis and prepare its concise entry packet."""
    actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"],
                  attempt_id=attempt["id"], fencing_token=attempt["fencing_token"])
    material = DeferredMaterial(hub.workspace(attempt["worker_id"]))

    def read(c: Any) -> dict:
        active, review = hub._active(c, actor)
        if review["contract"]["kind"] not in {"review"}:
            raise HubError("REVIEW_PROTOCOL", "A review entry requires an active review task")
        request = c.execute("SELECT * FROM review_requests WHERE task_id=?",
                            (review["id"],)).fetchone()
        if request is None:
            raise HubError("REVIEW_PROTOCOL", "The review task has no candidate review request")
        binding = hub._candidate_binding(c, request["candidate_id"])
        if not hub._can_review(c, review["id"], active["worker_id"]):
            raise HubError("REVIEWER_INELIGIBLE", "The review requires its active independent reviewer")
        artifact = hub._artifact(c, binding["artifact_id"])
        host = hub._task(c, binding["task_id"], binding["task_revision"])
        acceptance = hub._review_acceptance(c, review["id"])
        details = {"review_task": _task_details(review), "review_request": dict(request),
                   "candidate_binding": binding, "candidate": artifact, "host_task": _task_details(host),
                   "original_host_task": _task_details(hub._original_task(c, host["id"])),
                   "acceptance": acceptance}
        result = {"author_introduction": _author_introduction(material, review, artifact),
                  "candidate": _candidate_entry(artifact, material),
                  "host_task": host_entry(host, hub._conditions(c, host), material),
                  "review_request": {"task_id": review["id"], "phase": request["state"],
                                     "reviewer_id": active["worker_id"],
                                     "author_id": binding["author_id"], "coauthors": binding["coauthors"],
                                     "completion": bool(binding["completion"]),
                                     "basis": hub._review_basis(binding)},
                   "exact_details_file": material.json(_business_details(hub, c, details))}
        # The compact entry already supplies the exact record, acceptance, and
        # primary source references. Full evidence/history remains available on
        # demand without requiring a repeated read of every retained detail.
        if (artifact["kind"] not in MATHEMATICAL_KINDS
                or set(result["candidate"].get("primary_pair", {})) == {"lean", "markdown"}):
            result["exact_details_file"]["requires_read"] = False
        if acceptance is not None:
            result["review_request"]["acceptance"] = {
                key: acceptance[key] for key in (
                    "review_task_id", "candidate_id", "reviewer_id", "attempt_id", "created_at")}
        if request["state"] == "integrating":
            result["review_request"]["handoff"] = "Acceptance is recorded; the Integrator owns final integration and may ask for clarification. End this review turn."
        validation = hub._accepted_validation(c, binding)
        if validation is not None:
            # A review starts only after this; the guides say what the domain's validation checks.
            checks = json.loads(validation["details_json"]).get("checks")
            result["validation"] = {"verdict": validation["verdict"], "policy": validation["policy"],
                                    **({"checks": checks} if checks else {})}
        repository = hub._candidate_repository(c, binding["artifact_id"])
        if repository is not None:
            result["candidate"]["repository"] = _detail(material, repository)
        return result

    entry = await hub.db.call(read)
    await collected_thread_call(material.flush)  # The files, after the transaction and off its thread.
    return entry
