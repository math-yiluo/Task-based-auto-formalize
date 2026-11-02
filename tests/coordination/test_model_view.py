"""Operational metadata cannot become work instructions at native boundaries."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from formal_geyser.coordination.runtime.model_view import (
    project_packet,
    render_prompt,
    sync_view,
    tool_view,
)


def context(tmp_path):
    return {
        "packet_id": "backend-delivery", "worker_id": "Worker 0", "context_epoch": 9,
        "attempt": {"id": "backend-attempt", "fencing_token": 78},
        "task": {"id": "task-one", "revision": 1, "title": "Task one", "kind": "work",
                 "objective": "Exact user goal ∀ x.\nKeep this wording.",
                 "contract_hash": "internal-hash", "collaboration": {"runnable": 0, "active_attempt": "hidden"}},
        "native": {"workspace": str(tmp_path), "task_files": {"path": "tasks/current/task.json"},
                   "directories": {"library": "public/commit/files", "wiki": "public/commit/wiki",
                                   "knowledge_readme": "public/commit/wiki/README.md",
                                   "semantic_review_schema": ".private/reference/review.json"},
                   "lean_environment": {"environment_artifact": "business-environment", "library_roots": ["one", "two"]}},
        "instruction_guidance": {"directory": ".private/instructions", "files": {"worker.md": "private-digest"},
                                 "optional_files": {"worker-operations.md": "other-private-digest"}},
        "required_wiki": {"delivery_id": "backend-wiki", "full_read": True, "commit": "internal-commit",
                          "pages": [{"path": "wiki/start.md", "workspace_path": ".private/wiki-versions/abc/wiki/start.md",
                                     "sha256": "private-wiki-digest", "text": "NEVER INLINE THIS WIKI"}]},
        "human_messages": {"delivery_id": "backend-message", "messages": [{"id": "private-message-id",
                            "sender_role": "operator", "native_message": {"path": ".private/messages/one.json",
                            "sha256": "private-message-digest"}}]},
        "updates": [{"sequence": 10, "kind": "project.setup", "object_id": "backend-run"},
                    {"sequence": 11, "kind": "artifact.withdrawn", "object_id": "needed-artifact",
                     "payload": {"reason": "The earlier proof was invalidated.", "attempt_id": "private-attempt"}}],
        "after_sequence": 3, "through_sequence": 11, "usage": {"tokens_used": 90},
    }


def test_first_turn_has_exact_goal_and_readable_entries_without_internal_metadata(tmp_path):
    packet = context(tmp_path)
    original = deepcopy(packet)
    view = project_packet(packet)
    text = render_prompt(view)
    assert packet == original  # Runtime still owns its unmodified acknowledgment envelope.
    assert view["identity"] == "Worker 0"
    # The task's text lives in its file; a short objective is kept exactly as the excerpt.
    assert view["task"]["objective_excerpt"] == original["task"]["objective"]
    assert "objective" not in view["task"]
    assert view["paths"]["task"] == original["native"]["task_files"]["path"]
    for forbidden in ("backend-", "packet_id", "context_epoch", "fencing_token", "private-digest",
                      "private-wiki-digest", "private-message-digest", "NEVER INLINE THIS WIKI", "tokens_used",
                      "private-attempt"):
        assert forbidden not in text
    assert "wiki" not in view
    assert view["paths"]["knowledge_readme"] == "public/commit/wiki/README.md"
    # The required guide is part of the standing instructions; the input names on-demand guides only.
    assert view["reading"] == {"on_demand": [".private/instructions/worker-operations.md"]}
    assert ".private/instructions/worker.md" not in text
    assert view["messages"] == [{"sender_role": "operator", "sender": "User", "file": {"path": ".private/messages/one.json"}}]
    assert view["notifications"] == [{"event": "artifact.withdrawn", "reference": "needed-artifact",
                                       "summary": "artifact withdrawn: reason The earlier proof was invalidated."}]
    env = json.loads((tmp_path / view["paths"]["lean_environment"]).read_bytes())
    assert env == original["native"]["lean_environment"]
    refs = json.loads((tmp_path / view["paths"]["reference_index"]).read_bytes())
    assert refs["semantic_review_schema"] == ".private/reference/review.json"


def test_sync_and_compaction_reply_use_the_same_model_boundary(tmp_path):
    packet = context(tmp_path)
    assert tool_view(packet, workspace=tmp_path) == project_packet(packet)
    response = tool_view({"error": "Context changed", "context": packet}, workspace=tmp_path)
    assert response["context"]["task"]["objective_excerpt"] == packet["task"]["objective"]
    assert response["context"]["paths"]["task"] == packet["native"]["task_files"]["path"]
    assert "packet_id" not in render_prompt(response)
    assert "wiki" not in response["context"]
    # A sync returns only what is new, taken from the same projection.
    projected = project_packet(packet)
    synced = sync_view(packet)
    assert synced == {"task": {key: projected["task"][key] for key in ("id", "revision", "title")},
                      "notifications": projected["notifications"], "messages": projected["messages"]}
    assert "packet_id" not in render_prompt(synced)


def test_legacy_packet_keeps_the_complete_task_contract_entry(tmp_path):
    packet = context(tmp_path)
    packet["native"]["task_files"] = None
    packet["task"]["contract_material_path"] = ".private/materials/task-contract.json"
    packet["task"]["payload"] = {"context_material_path": "other.json"}
    view = project_packet(packet)
    assert view["paths"]["task"] == ".private/materials/task-contract.json"
    assert "details" not in view["task"]


def test_a_task_without_a_file_keeps_its_text_and_details_in_the_packet(tmp_path):
    packet = context(tmp_path)
    packet["native"]["task_files"] = None
    packet["task"].pop("contract_material_path", None)
    packet["task"]["payload"] = {"context_material_path": "other.json"}
    view = project_packet(packet)
    assert "task" not in view.get("paths", {})
    assert view["task"]["objective"] == packet["task"]["objective"] and view["task"]["details"]


def test_directed_message_projection_keeps_authority_and_readable_content(tmp_path):
    envelope = {"instruction": "New message", "human_messages": context(tmp_path)["human_messages"]}
    view = tool_view(envelope)
    assert view["messages"][0]["sender_role"] == "operator"
    assert view["messages"][0]["file"]["path"] == ".private/messages/one.json"
    assert "delivery_id" not in render_prompt(view)


@pytest.mark.parametrize("inbox", ["human_messages", "private_talk"])
def test_unread_message_pages_keep_a_sync_hint_without_transport_offsets(tmp_path, inbox):
    packet = context(tmp_path)
    packet[inbox] = {"messages": [], "has_more": True, "through_sequence": 93}
    view = project_packet(packet)
    assert "sync" in view["next_action"]
    assert "through_sequence" not in render_prompt(view)


def test_domain_evidence_and_authored_values_are_not_redacted_as_runtime_metadata():
    result = {"attempt_id": "private-attempt", "artifact_id": "accepted-result",
              "nested_status": {"packet_id": "private-packet", "state": "accepted"},
              "record": {"snapshot": "exact-source-hash", "data": {"packet_id": "authored mathematical label"}},
              "receipt": {"created_at": 17000, "attempt_id": "exact proving provenance"},
              "candidate_binding": {"attempt_id": "exact authored source binding"},
              "evidence": [{"policy": "lean.proof@1", "verdict": "accepted", "hash": "exact-checker-hash"}]}
    view = tool_view(result)
    assert "attempt_id" not in view
    assert view["nested_status"] == {"state": "accepted"}
    assert view["record"] == result["record"]
    assert view["receipt"] == result["receipt"]
    assert view["candidate_binding"] == result["candidate_binding"]
    assert view["evidence"] == result["evidence"]


def test_recovery_notifications_preserve_the_reason_without_native_provenance_ids(tmp_path):
    packet = context(tmp_path)
    packet["updates"] = [{"kind": "turn.terminal_reconciled", "object_id": "private-intent",
                          "payload": {"source_thread_id": "private-thread", "source_turn_id": "private-turn",
                                      "input_id": "private-input", "tokens_used": 123,
                                      "reason": "Confirmed that the stopped command finished.", "status": "completed"}}]
    view = project_packet(packet)
    assert view["notifications"] == [{"event": "turn.terminal_reconciled", "summary":
        "turn terminal reconciled: status completed; reason Confirmed that the stopped command finished."}]
    text = render_prompt(view)
    assert not any(value in text for value in ("private-intent", "private-thread", "private-turn",
                                               "private-input", "tokens_used"))
    error = tool_view({"error": "The assignment changed", "details": {
        "attempt_id": "private-attempt", "task_id": "needed-task", "revision": 3}})
    assert error["details"] == {"task_id": "needed-task", "revision": 3}


def test_reviewer_identity_messages_and_notifications_survive_the_model_boundary(tmp_path):
    packet = context(tmp_path)
    packet["worker_id"] = "internal-reviewer"
    packet["worker_identity"] = {"worker_id": "internal-reviewer", "display_name": "Reviewer 2",
                                 "worker_number": None, "reviewer_number": 2}
    packet["private_talk"] = {"messages": [{"sender_id": "Reviewer 1", "text": "Exact Reviewer 99 quotation."}]}
    packet["updates"] = [{"kind": "worker.recovered", "object_id": "Reviewer 2",
                          "payload": {"reason": "The saved review can continue.", "reviewer_number": 2}}]
    original = deepcopy(packet)
    view = project_packet(packet)
    assert packet == original
    assert view["identity"] == "Reviewer 2"
    assert view["messages"][-1] == {"sender": "Reviewer 1", "text": "Exact Reviewer 99 quotation."}
    assert view["notifications"] == [{"event": "worker.recovered", "worker": "Reviewer 2",
                                       "summary": "worker recovered: reason The saved review can continue."}]
    assert "reviewer_number" not in render_prompt(view) and "reviewer number" not in render_prompt(view)
    evidence = {"reviewer_number": 2, "worker_id": "internal-reviewer", "hash": "unaltered"}
    nested = tool_view({"members": [{"id": "Reviewer 2", "reviewer_number": 2, "worker_number": None}],
                        "receipt": evidence, "text": "Reviewer 99 remains a quotation."})
    assert nested["members"] == [{"id": "Reviewer 2"}]
    assert nested["receipt"] == evidence
    assert nested["text"] == "Reviewer 99 remains a quotation."
