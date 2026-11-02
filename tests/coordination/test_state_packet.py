"""The runtime's state packet describes a task without relying on the agent's memory."""
from __future__ import annotations

from formal_geyser.coordination.runtime import state_packet
from formal_geyser.coordination.runtime.handoff import HandoffCheck

STATE = {
    "task": {"id": "task-1", "title": "Valuation bound", "revision": 2, "state": "active",
             "contract_file": "tasks/task-1/task.md"},
    "members": [{"name": "Worker 3 (you)", "role": "first executor"},
                {"name": "Worker 5", "role": "collaborator", "current": False}],
    "candidates": [{"id": "c-1", "kind": "lean.proof", "status": "staged", "validation": "accepted",
                    "review": "pending"}],
    "messages": {"human": 1, "peer": 0},
    "wait": None,
    "checkpoint": {"summary": "Proved the first case.\nSecond case open.", "next_action": "Stage the pair."},
}
HISTORY = {"files": [{"path": "Valuation/Bound.lean", "use": "edited"}],
           "replies": [{"text": "Compiling the second case."}],
           "errors": [{"operation": "stage", "code": "STALE_REVISION", "error": "Revise first"},
                      {"tool": "commandExecution", "exit_code": 1, "command": "grep -n sorry X.lean"}]}


def test_the_state_packet_lists_records_files_replies_and_errors(tmp_path):
    text = state_packet.render(state=STATE, history=HISTORY, reason="context", mode="aggressive", handoff=None)
    assert text.startswith("# State of task: Valuation bound\n")
    assert "the conversation's context grew large; rotation mode: aggressive" in text
    assert "- Contract: `tasks/task-1/task.md`" in text and "- Worker 5: collaborator (left)" in text
    assert "- `c-1` lean.proof: staged; machine validation accepted; review pending" in text
    assert "- 1 unread human messages; they arrive with your next input." in text
    assert "> Proved the first case.\n> Second case open." in text and "Next action: Stage the pair." in text
    assert "- `Valuation/Bound.lean` (edited)" in text and "> Compiling the second case." in text
    assert "- stage: STALE_REVISION (Revise first)" in text
    assert "- commandExecution: exit code 1 (grep -n sorry X.lean)" in text
    relative = state_packet.write(tmp_path, "task-1", text)
    assert relative == ".private/tasks/task-1/state.md"
    assert (tmp_path / relative).read_text(encoding="utf-8") == text


def test_an_unusable_handoff_is_named_and_an_empty_task_says_so():
    broken = HandoffCheck(".private/tasks/task-1/handoff.md", False, None, 0, ("The handoff file does not exist",))
    state = {**STATE, "candidates": [], "members": [], "messages": {}, "checkpoint": None}
    text = state_packet.render(state=state, history={}, reason="idle", mode="conservative", handoff=broken)
    assert ("**Your handoff file `.private/tasks/task-1/handoff.md` needs attention:** "
            "The handoff file does not exist.") in text
    assert "## Your candidates for this task\n\n- None." in text and "## Team" not in text
