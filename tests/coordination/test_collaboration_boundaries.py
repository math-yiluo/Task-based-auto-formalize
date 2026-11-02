"""Independent publication, dependency, and recovery boundary regressions."""
from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.coordination.monitoring import read_database
from formal_geyser.coordination.plugins import load_plugin
from tests.coordination.lifecycle_helpers import finish_ordinary, select_review
from tests.coordination.publication_helpers import file_payload, new_hub, publish_batch

OP = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)
VALIDATOR = Actor(role=Role.VALIDATOR)


async def command(hub, name, payload=None, actor=OP, key=None):
    return await hub.execute(Command(name=name, payload=payload or {}, key=key or uuid4().hex), actor)


@pytest.fixture
async def hub(tmp_path):
    result = await new_hub(tmp_path)
    yield result
    await result.close()


async def publish(hub, title, **contract):
    return await command(hub, "task.publish", {"contract": {
        "title": title, "objective": "Produce and review a bounded software record.", **contract}})


async def start(hub):
    return await command(hub, "run.start")


async def claim(hub, task, run):
    if task["contract"]["kind"] == "review":
        attempt = await select_review(hub, task["id"], run["id"])
        return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                     fencing_token=attempt["fencing_token"])
    worker = await command(hub, "worker.register")
    attempt = await command(hub, "task.claim", {"task_id": task["id"], "revision": task["revision"],
                                               "worker_id": worker["id"], "run_id": run["id"]})
    return Actor(role=Role.WORKER, worker_id=worker["id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


async def quiet(hub, actor):
    snapshot = hub.blobs.snapshot(hub.workspace(actor.worker_id))
    return await command(hub, "runtime.quiescent", {
        "worker_id": actor.worker_id, "snapshot": snapshot}, RUNNER)


async def machine_accept(hub, candidate):
    task = await hub.task(candidate["task_id"])
    report = await load_plugin("general").validate_candidate(hub, task, candidate)
    assert report.verdict == "accepted"
    return await command(hub, "artifact.accept", {"artifact_id": candidate["id"], **report.model_dump()}, VALIDATOR)


async def publish_accepted(hub, reviewer, run):
    """End the accepted reviewer's turn and publish it in one Integrator batch."""
    await hub.ordinary_turn_completed(reviewer.worker_id, reviewer.attempt_id)
    await publish_batch(hub, run["id"])


async def peer_accept(hub, candidate, run, *, publish=True):
    review = await hub.task(candidate["review_task_id"])
    reviewer = await claim(hub, review, run)
    binding = review["collaboration"]["review"]
    await command(hub, "review.accept", {
        **{key: binding[key] for key in ("candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")},
        "verdict": "approve", "rationale": "The exact bounded record meets the requested claim.",
        "evidence": ["Checked the candidate fields against its fixed objective."], "qualifications": [],
    }, reviewer)
    if publish:
        await publish_accepted(hub, reviewer, run)
    return reviewer


@pytest.mark.parametrize("peer_first", [False, True])
async def test_partial_publication_does_not_satisfy_task_completion_dependency(hub, peer_first):
    host = await publish(hub, "Shared producer")
    run = await start(hub)
    author = await claim(hub, host, run)
    candidate = await command(hub, "artifact.stage", file_payload(
        hub, author, kind="record", data={"partial": "A useful component"}), author)
    if peer_first:
        reviewer = await peer_accept(hub, candidate, run, publish=False)
        assert not (await hub.artifact(candidate["id"]))["accepted"]
        await machine_accept(hub, candidate)
        await publish_accepted(hub, reviewer, run)
    else:
        await machine_accept(hub, candidate)
        assert not (await hub.artifact(candidate["id"]))["accepted"]
        await peer_accept(hub, candidate, run)
    assert (await hub.artifact(candidate["id"]))["accepted"]
    assert (await hub.task(host["id"]))["state"] != "done"
    consumer = await publish(hub, "Consumer of the completed task", requirements=[{
        "key": "complete-result", "task_id": host["id"], "task_revision": host["revision"],
        "policy": host["contract"]["acceptance_policy"]}])
    with pytest.raises(HubError) as missing:
        await claim(hub, consumer, run)
    assert missing.value.code == "PREREQUISITES_MISSING"
    snapshot = read_database(hub.root, run["id"])
    observed = next(task for task in snapshot["tasks"] if task["id"] == consumer["id"])
    assert not observed["requirements_satisfied"]
    partial_consumer = await publish(hub, "Consumer of the explicit partial artifact", requirements=[{
        "key": "component", "artifact_id": candidate["id"], "policy": host["contract"]["acceptance_policy"]}])
    assert (await claim(hub, partial_consumer, run)).attempt_id


async def legacy_publish(hub, monkeypatch, title, **contract):
    """Publish as the hub did until 2026-10-22, when an objective could be only a file name."""
    with monkeypatch.context() as patched:
        patched.setattr(type(hub), "_require_contract_text", staticmethod(lambda contract: None))
        return await publish(hub, title, objective="objective.md", **contract)


async def name_target(hub, task, **changes):
    """The publisher's corrected contract, with the objective's text."""
    contract = {**task["contract"], "objective": "Produce and review a bounded software record.", **changes}
    return await command(hub, "task.revise", {"task_id": task["id"], "revision": task["revision"], "contract": contract,
                                              "reason": "The corrected contract names its target."})


async def test_a_first_revision_that_named_no_target_is_not_the_original_contract(hub, monkeypatch):
    # Worker 318's accepted proof could not complete its task: the task's first revision, and the
    # dependency revision that one pinned, had stored "objective.md" as their objective.
    await publish(hub, "Task 0")  # Completing it would stop the project.
    host = await name_target(hub, await legacy_publish(hub, monkeypatch, "Shared producer"))
    policy = host["contract"]["acceptance_policy"]
    consumer = await legacy_publish(hub, monkeypatch, "Consumer", requirements=[
        {"key": "result", "task_id": host["id"], "task_revision": 1, "policy": policy}])
    consumer = await name_target(hub, consumer, requirements=[
        {"key": "result", "task_id": host["id"], "task_revision": host["revision"], "policy": policy}])
    pinned = await publish(hub, "Consumer pinned to the first revision", requirements=[
        {"key": "result", "task_id": host["id"], "task_revision": 1, "policy": policy}])
    run = await start(hub)
    for task in (host, consumer):
        author = await claim(hub, task, run)
        candidate = await command(hub, "artifact.stage", file_payload(
            hub, author, kind="record", data={"result": task["contract"]["title"]}), author)
        await machine_accept(hub, candidate)
        await peer_accept(hub, candidate, run)
        conditions = await hub.db.call(lambda c, task_id=task["id"]: hub._conditions(c, hub._task(c, task_id)))
        assert conditions["objective"] == task["contract"]["objective"] and "current-objective" not in conditions
        await finish_ordinary(hub, task["id"], author, artifact_ids=[candidate["id"]])
        await quiet(hub, author)
        assert (await hub.task(task["id"]))["state"] == "done"
    assert (await claim(hub, pinned, run)).attempt_id  # Revision 1 of the producer means its revision 2.


async def test_staging_retry_does_not_duplicate_review_and_review_must_bind_inputs(hub):
    host, run = await publish(hub, "Immutable contribution"), await start(hub)
    author = await claim(hub, host, run)
    payload = file_payload(hub, author, kind="record", data={"component": "same immutable content"})
    candidate = await command(hub, "artifact.stage", payload, author, key="stage-once")
    replay = await command(hub, "artifact.stage", payload, author, key="stage-once")
    assert replay["id"] == candidate["id"]
    reviews = [task for task in await hub.list_tasks() if task["contract"]["kind"] == "review"]
    assert len(reviews) == 1
    with pytest.raises(HubError):
        await command(hub, "task.claim", {"task_id": reviews[0]["id"], "revision": 1,
                                          "worker_id": author.worker_id, "run_id": run["id"]})
    reviewer = await claim(hub, reviews[0], run)
    binding = reviews[0]["collaboration"]["review"]
    with pytest.raises(HubError) as wrong:
        await command(hub, "review.accept", {
            **{key: binding[key] for key in ("candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")},
            "candidate_hash": "0" * 64, "verdict": "approve", "rationale": "Incorrect binding.",
            "evidence": ["A claim about another candidate."], "qualifications": [],
        }, reviewer)
    assert wrong.value.code == "REVIEW_BINDING_MISMATCH"
    assert (await hub.worker(reviewer.worker_id))["state"] == "running"
    assert not (await hub.artifact(candidate["id"]))["accepted"]


async def test_operator_validation_cannot_make_private_record_public(hub):
    host, run = await publish(hub, "Private diagnostic isolation"), await start(hub)
    author = await claim(hub, host, run)
    private = await command(hub, "record.private", {"kind": "diagnostic", "data": {"log": "Local observation"}}, author)
    with pytest.raises(HubError) as denied:
        await command(hub, "artifact.accept", {"artifact_id": private["id"], "policy": "general.peer@1",
                                               "verdict": "accepted", "details": {}, "qualifications": []})
    assert denied.value.code == "PRIVATE_RECORD"
    assert (await hub.artifact(private["id"]))["status"] == "private"
    assert len(await hub.list_tasks()) == 1
