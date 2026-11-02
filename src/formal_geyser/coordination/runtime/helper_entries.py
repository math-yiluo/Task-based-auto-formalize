"""Small, self-contained decision prompts: the decision model has no tools and reads no files."""
from __future__ import annotations

from typing import Any

OBJECTIVE_CHARACTERS = 2000
PROGRESS_CHARACTERS = 1500

ASSIGNMENT_INSTRUCTION = (
    "A task needs a worker. Choose the idle worker whose previous tasks make them best suited "
    "to it, copying their name exactly as listed, or null when none suits it better than a new "
    "worker would. A new worker starts without their experience but also without unrelated "
    "history. Answer only {\"worker\": name or null}.")
RECRUITMENT_INSTRUCTION = (
    "One worker has started this task. Decide whether it should also get a second worker: true "
    "only for a separable part that a second worker could pursue in parallel, false otherwise. "
    "Answer only {\"decision\": true or false}.")


def _excerpt(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else ""
    return text if len(text) <= limit else text[:limit] + " [truncated]"


def _task(task: dict) -> dict:
    contract = task["contract"]
    return {"title": contract["title"], "kind": contract["kind"],
            "objective": _excerpt(contract["objective"], OBJECTIVE_CHARACTERS)}


def assignment_entry(task: dict, offer: dict) -> dict:
    """The task and, for each idle worker, only the titles of the tasks they executed."""
    described = _task(task)
    if offer.get("opportunity") == "collaborator":  # The task's second executor, with its invitation.
        described["collaborator_place"] = _excerpt(offer.get("invitation"), PROGRESS_CHARACTERS)
    return {"instruction": ASSIGNMENT_INSTRUCTION, "task": described,
            "idle_workers": [{"name": item["name"], "previous_tasks": list(item["previous_tasks"])}
                             for item in offer["workers"]]}


def recruitment_entry(task: dict, progress: dict | None = None) -> dict:
    """The task, the first executor's latest saved checkpoint and the state and plan of its
    handoff file, when it wrote them."""
    result: dict[str, Any] = {"instruction": RECRUITMENT_INSTRUCTION, "task": _task(task)}
    checkpoint = (progress or {}).get("checkpoint")
    if isinstance(checkpoint, dict):
        result["progress"] = {key: _excerpt(value, PROGRESS_CHARACTERS)
                              for key, value in checkpoint.items() if isinstance(value, str)}
    handoff = (progress or {}).get("handoff")
    if isinstance(handoff, dict):
        result["handoff"] = {key: _excerpt(value, PROGRESS_CHARACTERS) for key, value in handoff.items()}
    return result
