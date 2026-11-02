"""Conservative worker views; the complete coordination ledger remains intact."""
from __future__ import annotations

from typing import Any

WORKER_UPDATE_LIMIT = 200
# The runtime's own records about a worker's conversation; the runtime already acted on them.
OWN_CONVERSATION_BOOKKEEPING = frozenset({
    "worker.context_compacted", "worker.conversation_released", "worker.rotation_mode_selected",
    "rotation.requested", "rotation.ready", "rotation.completed", "rotation.handoff_repair_requested",
    "review.report_recorded",
    # The worker's own pause: it chose it.
    "continuation.created", "worker.suspended",
})


def routine_for_recipient(kind: str, payload: Any, event_worker_id: str | None,
                          recipient_id: str) -> bool:
    """Omit only known successful bookkeeping about this recipient itself.

    Peers' claims can signal new collaboration. Unknown kinds, added fields,
    failed/interrupted turns, and uncertain process state remain visible.
    """
    if event_worker_id != recipient_id or not isinstance(payload, dict):
        return False
    if kind in OWN_CONVERSATION_BOOKKEEPING:
        return True
    if kind == "task.claimed":
        return (set(payload) == {"attempt_id", "fencing_token"}
                and isinstance(payload["attempt_id"], str) and bool(payload["attempt_id"])
                and type(payload["fencing_token"]) is int and payload["fencing_token"] > 0)
    if kind == "turn.finished":
        return (set(payload) == {"status", "terminal_observed", "process_quiet"}
                and payload["status"] == "completed"
                and payload["terminal_observed"] is True and payload["process_quiet"] is True)
    return False


def worker_collaboration(collaboration: dict) -> dict:
    """Remove helper correlation IDs, retaining every recruitment decision."""
    result = dict(collaboration)
    recruitment = result.get("recruitment")
    if isinstance(recruitment, dict):
        result["recruitment"] = {key: value for key, value in recruitment.items()
                                 if key not in {"first_turn_id", "decision_helper_id"}}
    return result
