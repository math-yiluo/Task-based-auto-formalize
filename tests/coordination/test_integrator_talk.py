"""Integrator clarification uses the ordinary one-conversation-per-pair talk."""

import pytest

from formal_geyser.coordination.batch_integration import BATCH_DELAY_MS
from formal_geyser.coordination.errors import HubError
from tests.coordination.test_collaborative_core import command, worker_actor
from tests.coordination.test_integrator_batches import batch_case as batch_case
from tests.coordination.test_integrator_batches import choose, ready


async def test_original_reviewer_answers_in_place_without_rejoining_integration(batch_case):
    hub, run, now = batch_case
    reviewer = await ready(hub, run, "Clarification fixture")
    now[0] += BATCH_DELAY_MS
    integrator = await choose(hub, run)
    batch = await hub.db.call(lambda c: hub._batch(c))
    artifact = batch["items"][0]["candidate_id"]
    question = await command(
        hub,
        "talk.send",
        {
            "recipient_id": reviewer["worker_id"],
            "message_id": "integrator-question",
            "text": f"Artifact {artifact}: which source paragraph fixes this normalization?",
        },
        worker_actor(integrator),
    )
    recipient = await hub.worker(reviewer["worker_id"])
    assert recipient["pending_wake"]
    answer = await hub.db.call(
        lambda c: hub._message_assignment(
            c, hub._get(c, "workers", reviewer["worker_id"]), run["id"]
        ),
        write=True,
    )
    assert answer["worker_id"] == reviewer["worker_id"]
    assert answer["execution_mode"] == "message_only"
    inbox = await hub.peer_unread(worker_actor(answer), reference_only=True)
    assert inbox["messages"]
    reply = await command(
        hub,
        "talk.send",
        {
            "recipient_id": "Integrator",
            "message_id": "reviewer-answer",
            "text": f"Artifact {artifact}: use the cited normalization paragraph; the convention is unchanged.",
        },
        worker_actor(answer),
    )
    assert reply["conversation_id"] == question["conversation_id"]
    await hub.ordinary_turn_completed(answer["worker_id"], answer["id"])
    assert (await hub.worker(answer["worker_id"]))["work_intention"] == "waiting"
    assert (
        await hub.db.call(
            lambda c: c.execute("select count(*) from workers where kind='integrator'").fetchone()[
                0
            ]
        )
        == 1
    )


async def test_integrator_project_contact_does_not_reopen_a_withdrawn_artifact(batch_case):
    hub, run, now = batch_case
    await ready(hub, run, "Scoped contribution")
    unrelated = await command(hub, "worker.register", {})
    now[0] += BATCH_DELAY_MS
    integrator = await choose(hub, run)
    batch = await hub.db.call(lambda c: hub._batch(c))
    artifact = batch["items"][0]["candidate_id"]
    payload = {"recipient_id": unrelated["id"], "message_id": "clarify", "text": f"Question about {artifact}."}
    sent = await command(hub, "talk.send", payload, worker_actor(integrator))
    assert sent["recipient_id"] == unrelated["id"]
    for context in ({"artifact_id": artifact}, {"task_id": "missing"}):
        with pytest.raises(HubError) as error:
            await command(hub, "talk.send", {**payload, "message_id": "scoped", **context}, worker_actor(integrator))
        assert error.value.code == "INVALID_INPUT"
    await command(hub, "artifact.withdraw", {"artifact_id": artifact, "reason": "The author abandoned this route."})
    assert (await hub.artifact(artifact))["status"] == "withdrawn"
