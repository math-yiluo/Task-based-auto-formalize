"""Scenario views of context; delivery receipts still cover every supplied event."""
from __future__ import annotations

import json

from ..material_files import MaterialFiles
from ..models import Actor, Role
from ..storage import canonical

MESSAGE_MODES = frozenset({"message", "message_only"})


async def message_state(hub, attempt: dict) -> dict:
    """Read current intention and the exact saved wait, without changing either."""
    actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"],
                  attempt_id=attempt["id"], fencing_token=attempt["fencing_token"])

    def read(c):
        active, _ = hub._active(c, actor)
        worker = hub._get(c, "workers", actor.worker_id)
        assigned = active["execution_mode"] != "message_only"
        result = {
            "task_execution_authority": assigned,
            "after_turn_intention": worker["work_intention"],
            "resume_tool": "taskhub_call",
            "resume_operation": "resume_work",
            "resume_effect": "continue_assigned_task" if assigned else "become_idle",
        }
        if active["continuation_id"]:
            saved = hub._get(c, "continuations", active["continuation_id"])
            result["saved_wait"] = {
                "id": saved["id"], "state": saved["state"],
                "predicate": json.loads(saved["predicate_json"]),
            }
        return result

    result = await hub.db.call(read)
    if "saved_wait" in result and len(canonical(result["saved_wait"]).encode("utf-8")) > 2048:
        # Predicates may include exact external-response/completion IDs. Never
        # summarize them or infer fulfillment from a directed message.
        result["saved_wait"] = MaterialFiles(hub.workspace(attempt["worker_id"])).json(result["saved_wait"])
    return result


def scenario_entry(packet: dict, *, full_context: bool = False) -> dict:
    """Reduce repeated state only; keep exact updates and directed text.

    The coordinator acknowledges packet_id after transport. Dropping any update
    here would incorrectly advance its cursor past information never delivered.
    Recovery and compaction keep the complete entry (`full_context`).
    """
    review = packet.get("review")
    candidate_review = isinstance(review, dict)
    if candidate_review:
        # The immutable review contract is retained on disk. The introduction
        # and current review state have one canonical entry, including recovery;
        # old duplicate collaboration phases must not outlive a later delta.
        task = {key: value for key, value in packet["task"].items()
                if key not in {"context", "payload", "collaboration", "context_material_path", "context_material_sha256",
                               "payload_material_path", "payload_material_sha256"}}
        if "After acceptance, the same reviewer integrates" in task.get("objective", ""):
            task["objective"] = "Independently review the exact candidate. Acceptance hands integration to the Integrator; it does not publish."
        packet = {**packet, "task": task}
    message = packet.get("execution_mode") in MESSAGE_MODES
    if full_context or (not message and not candidate_review):
        return packet

    result = {key: packet[key] for key in (
        "packet_id", "worker_id", "worker_identity", "context_epoch", "execution_mode",
        "instruction_guidance", "continuation_instruction", "message_state", "review", "audit_anchor",
        "rotation", "review_report",
    ) if key in packet}
    result["task"] = {key: value for key, value in packet["task"].items()
                      if key in {"id", "revision", "title", "kind", "state", "contract_hash"}}
    native = packet.get("native", {})
    result["native"] = {key: value for key, value in native.items()
                        if key in {"mode", "workspace", "task_files", "task_drafts", "shared_dags", "read_only_resources", "library", "lean_environment"}
                        and value is not None}
    result["native"]["directories"] = {key: value for key, value in native.get("directories", {}).items()
        if key in {"instructions", "drafts", "graphs", "dag_checker", "library", "wiki", "knowledge_readme", "public_notes", "library_wiki", "literature", "literature_index", "literature_wiki",
                   "central_literature", "private_notes", "submission_guide", "submission_schemas",
                   "operation_guide", "operation_schemas", "operation_examples",
                   "semantic_review_guide", "semantic_review_schema", "semantic_review_example"}}
    for key in ("checkpoint", "inputs", "requirements", "resume_requirements", "updates",
                "private_talk", "human_messages", "requires_sync", "has_more_updates"):
        if (packet.get("execution_mode") == "message_only"
                and key in {"checkpoint", "inputs", "requirements", "resume_requirements"}):
            continue  # These belong to a historical anchor, not assigned work.
        if packet.get(key):
            result[key] = packet[key]
    return result
