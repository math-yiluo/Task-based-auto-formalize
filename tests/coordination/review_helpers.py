"""Accepted-review fixtures shared by integrator tests."""
from tests.coordination.lifecycle_helpers import select_review
from tests.coordination.test_collaborative_core import (
    claim,
    command,
    machine,
    publish,
    stage,
    worker_actor,
)


def basis(reviewer):
    binding = reviewer["task"]["collaboration"]["review"]
    return {**{key: binding[key] for key in ("candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")},
            "rationale": "Compared the exact candidate with its task.", "evidence": ["Exact candidate files."],
            "qualifications": []}


async def reviewing(hub, run, title):
    """An independent reviewer in its review phase for a machine-validated candidate."""
    author = await claim(hub, await publish(hub, title), run)
    candidate = await stage(hub, author)
    await machine(hub, candidate)
    return await select_review(hub, candidate["review_task_id"], run["id"])


async def accept(hub, reviewer):
    return await command(hub, "review.accept", basis(reviewer), worker_actor(reviewer))
