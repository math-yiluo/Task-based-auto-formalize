"""Decision prompts are small and self-contained: the decision model reads no files."""
from __future__ import annotations

import json

from formal_geyser.coordination.runtime.helper_entries import (
    OBJECTIVE_CHARACTERS,
    PROGRESS_CHARACTERS,
    assignment_entry,
    recruitment_entry,
)


def task(objective="Prove the lemma.", kind="ordinary"):
    return {"id": "task-1", "revision": 3, "files": {"path": "/private/tasks/task-1.json"},
            "contract": {"title": "Lemma task", "kind": kind, "objective": objective,
                         "acceptance_policy": "general.json@1", "payload": {"secret": "not shown"}}}


def offer(**changes):
    return {"task_revision": 3, "opportunity": "initial", "invitation": "",
            "workers": [{"worker_id": "id-7", "name": "Worker 7", "previous_tasks": ["Earlier lemma", "Older"]},
                        {"worker_id": "id-9", "name": "Worker 9", "previous_tasks": []}], **changes}


def test_assignment_entry_shows_the_task_and_only_idle_workers_names_and_titles():
    entry = assignment_entry(task(), offer())
    assert entry["task"] == {"title": "Lemma task", "kind": "ordinary", "objective": "Prove the lemma."}
    assert entry["idle_workers"] == [{"name": "Worker 7", "previous_tasks": ["Earlier lemma", "Older"]},
                                     {"name": "Worker 9", "previous_tasks": []}]
    text = json.dumps(entry)
    for hidden in ("id-7", "task-1", "/private/", "secret", "general.json@1"):
        assert hidden not in text  # No identifiers, files or payloads: nothing to read with tools.


def test_assignment_entry_names_a_collaborator_opening_and_truncates_long_text():
    entry = assignment_entry(task("x" * (OBJECTIVE_CHARACTERS + 50)),
                             offer(opportunity="collaborator", invitation="Check the converse direction."))
    assert entry["task"]["objective"] == "x" * OBJECTIVE_CHARACTERS + " [truncated]"
    assert entry["task"]["collaborator_place"] == "Check the converse direction."


def test_recruitment_entry_shows_the_task_and_the_latest_checkpoint_text():
    entry = recruitment_entry(task(), {"task_revision": 3, "checkpoint": {
        "summary": "s" * (PROGRESS_CHARACTERS + 5), "next_action": "Try induction.", "attempt": 2}})
    assert entry["task"]["title"] == "Lemma task"
    assert entry["progress"] == {"summary": "s" * PROGRESS_CHARACTERS + " [truncated]",
                                 "next_action": "Try induction."}
    assert "progress" not in recruitment_entry(task())
