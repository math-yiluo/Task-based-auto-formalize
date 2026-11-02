"""Attribution does not require or confer execution-team membership."""
import pytest

from formal_geyser.coordination.errors import HubError
from tests.coordination.lifecycle_helpers import select_review
from tests.coordination.publication_helpers import file_payload, new_hub
from tests.coordination.test_collaborative_core import (
    claim,
    command,
    publish,
    quiet,
    review,
    stage,
    start,
    worker_actor,
)


@pytest.fixture
async def hub(tmp_path):
    active = await new_hub(tmp_path)
    try:
        yield active
    finally:
        await active.close()


@pytest.mark.parametrize("is_publisher", [False, True])
async def test_nonmember_coauthor_preserves_identity_and_review_independence(hub, is_publisher):
    parent_task, run = await publish(hub, "Original work"), await start(hub)
    parent = await claim(hub, parent_task, run)
    options = {"actor": worker_actor(parent)} if is_publisher else {}
    task = await publish(hub, "Finish the supplied draft", **options)
    author = await claim(hub, task, run)
    before = (await hub.task(task["id"]))["collaboration"]
    coauthor = await hub.worker(parent["worker_id"])
    candidate = await stage(hub, author, coauthors=[f"Worker {coauthor['worker_number']}"])
    after = (await hub.task(task["id"]))["collaboration"]
    assert before["members"] == after["members"]
    assert before["publishers"] == after["publishers"]
    assert parent["worker_id"] not in {member["worker_id"] for member in after["members"]}
    binding = (await hub.task(candidate["review_task_id"]))["collaboration"]["review"]
    assert binding["coauthors"] == [parent["worker_id"]]
    for excluded in (author["worker_id"], parent["worker_id"]):
        assert not await hub.db.call(lambda c: hub._can_review(c, candidate["review_task_id"], excluded))
    outsider = await command(hub, "worker.register")
    assert await hub.db.call(lambda c: hub._can_review(c, candidate["review_task_id"], outsider["id"]))


async def test_unknown_coauthor_is_rejected_without_staging(hub):
    task, run = await publish(hub), await start(hub)
    author = await claim(hub, task, run)
    before = len(await hub.list_artifacts())
    with pytest.raises(HubError) as error:
        await stage(hub, author, coauthors=["Worker 999999"])
    assert error.value.code == "NOT_FOUND"
    assert len(await hub.list_artifacts()) == before


async def test_correction_retains_nonmember_coauthors(hub):
    task, run = await publish(hub), await start(hub)
    author = await claim(hub, task, run)
    coauthor = await command(hub, "worker.register")
    candidate = await stage(hub, author, coauthors=[coauthor["id"]])
    reviewer = await select_review(hub, candidate["review_task_id"], run["id"])
    await review(hub, reviewer, verdict="request_changes")
    await quiet(hub, reviewer)
    corrected = await command(hub, "artifact.stage", file_payload(
        hub, author, data={"corrected": True}, replaces=candidate["artifact_id"]), worker_actor(author))
    binding = (await hub.task(corrected["review_task_id"]))["collaboration"]["review"]
    assert coauthor["id"] in binding["coauthors"]
    assert not await hub.db.call(lambda c: hub._can_review(c, corrected["review_task_id"], coauthor["id"]))


async def test_review_acceptance_still_rejects_unresolved_reviewer_qualifications(hub):
    task, run = await publish(hub), await start(hub)
    author = await claim(hub, task, run)
    candidate = await stage(hub, author)
    reviewer = await select_review(hub, candidate["review_task_id"], run["id"])
    binding = reviewer["task"]["collaboration"]["review"]
    payload = {key: binding[key] for key in ("candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")}
    payload.update(rationale="One verification remains unresolved.", evidence=["Exact source inspected."],
                   qualifications=["The task's required source is unavailable."])
    with pytest.raises(HubError) as error:
        await command(hub, "review.accept", payload, worker_actor(reviewer))
    assert error.value.code == "REVIEW_QUALIFIED"
