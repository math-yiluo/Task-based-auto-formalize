"""The state packet's working set and history search read a worker's recorded transcript."""
from __future__ import annotations

import json

from formal_geyser.coordination.transcript_digest import digest, search


def write(path, events):
    path.write_text("".join(json.dumps({"at": f"2026-10-19T0{index % 10}:00:00Z", **event}) + "\n"
                            for index, event in enumerate(events)) + "{not json}\n", encoding="utf-8")


def gateway(thread, operation, arguments, status="running", **fields):
    return {"type": "action", "thread_id": thread, "tool": "taskhub_call", "status": status,
            "operation": operation, "arguments": {"operation": operation, "arguments": arguments}, **fields}


def test_the_digest_lists_recent_files_replies_and_errors_of_one_thread(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, [
        {"type": "assistant", "thread_id": "old", "text": "Earlier conversation."},
        gateway("current", "workspace_read", {"path": "notes/plan.md"}),
        {"type": "action", "thread_id": "current", "tool": "fileChange", "status": "completed",
         "details": {"paths": ["Valuation/Bound.lean"]}},
        {"type": "action", "thread_id": "current", "tool": "commandExecution", "status": "inProgress",
         "details": {"command": "grep -n sorry Valuation/Bound.lean && cat notes/plan.md"}},
        {"type": "action", "thread_id": "current", "tool": "commandExecution", "status": "completed",
         "details": {"command": "grep -c sorry Valuation/Bound.lean", "exitCode": 1}},
        gateway("current", "stage", {"path": "drafts/pair"}),
        gateway("current", "stage", {}, status="error", result={"code": "STALE_REVISION", "error": "Revise first"}),
        {"type": "assistant", "thread_id": "current", "text": "First reply."},
        {"type": "assistant", "thread_id": "current", "text": "x" * 900},
    ])
    result = digest(path, "current", replies=1)
    assert [(item["path"], item["use"]) for item in result["files"]] == [
        ("drafts/pair", "written"), ("notes/plan.md", "used in a command"), ("Valuation/Bound.lean", "edited")]
    assert result["replies"][0]["text"].endswith(" [truncated]") and len(result["replies"]) == 1
    assert [error.get("code") or error.get("exit_code") for error in result["errors"]] == [1, "STALE_REVISION"]


def test_history_search_finds_every_word_newest_first_outside_the_current_thread(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, [
        {"type": "assistant", "thread_id": "first", "text": "The padic valuation lemma needs Nat.find."},
        {"type": "input", "thread_id": "first", "text": "Please check the padic VALUATION bound."},
        gateway("second", "workspace_read", {"path": "notes/padic-valuation.md"}),
        {"type": "assistant", "thread_id": "current", "text": "padic valuation again"},
    ])
    found = search(path, "Padic valuation", limit=2, exclude_thread="current")
    assert [(item["conversation"], item["kind"]) for item in found["matches"]] == [
        ("second", "your action"), ("first", "message to you")]
    assert found["truncated"] is True
    assert search(path, "   ")["matches"] == [] and search(tmp_path / "missing.jsonl", "x")["matches"] == []
