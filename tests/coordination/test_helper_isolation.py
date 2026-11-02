"""One unreadable task or worker cannot withhold decision helpers from the others."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from formal_geyser.coordination.models import Actor, Command, Role
from tests.coordination.lifecycle_helpers import assignment_decision, idle_worker
from tests.coordination.publication_helpers import new_hub

OPERATOR = Actor(role=Role.OPERATOR)


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), OPERATOR)


async def skipped_events(hub, subject):
    return await hub.db.call(lambda c: c.execute(
        "SELECT payload_json FROM events WHERE kind='admission.item_skipped' AND object_id=?",
        (subject,)).fetchall())


def corrupt(task):
    path = Path(task["files"]["path"])
    original = path.read_bytes()
    path.chmod(0o644)
    path.write_bytes(original + b" ")
    return lambda: path.write_bytes(original)


async def two_pending_recruitments(hub):
    """Two first turns end while the run is paused, so neither helper exists yet."""
    run = await command(hub, "run.start")
    tasks = []
    for title in ("first", "second"):
        task = await command(hub, "task.publish", {"contract": {
            "title": title, "objective": f"Inspect the {title} fixture."}})
        worker = await command(hub, "worker.register")
        attempt = await command(hub, "task.claim", {"task_id": task["id"], "revision": task["revision"],
                                                    "run_id": run["id"], "worker_id": worker["id"]})
        tasks.append((task, attempt))
    await command(hub, "run.pause", {"run_id": run["id"]})
    for _, attempt in tasks:
        await hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    await command(hub, "run.resume", {"run_id": run["id"]})
    return run, tasks


async def test_unreadable_task_does_not_withhold_other_recruitment_helpers(tmp_path):
    hub = await new_hub(tmp_path / "project")
    try:
        run, [(healthy, _), (broken, _)] = await two_pending_recruitments(hub)
        restore = corrupt(broken)

        helpers = await hub.due_recruitment_intents(run["id"])
        assert [helper["source_task_id"] for helper in helpers] == [healthy["id"]]
        assert len(await skipped_events(hub, broken["id"])) == 1
        # The fault is reported once while it persists.
        assert [h["source_task_id"] for h in await hub.due_recruitment_intents(run["id"])] == [healthy["id"]]
        assert len(await skipped_events(hub, broken["id"])) == 1

        restore()
        helpers = await hub.due_recruitment_intents(run["id"])
        assert {helper["source_task_id"] for helper in helpers} == {healthy["id"], broken["id"]}
    finally:
        await hub.close()


async def test_unreadable_task_does_not_withhold_the_assignment_decision_from_others(tmp_path):
    hub = await new_hub(tmp_path / "project")
    try:
        run = await command(hub, "run.start")
        idle = await idle_worker(hub, run, "Earlier task")
        broken, healthy = [await command(hub, "task.publish", {"contract": {
            "title": title, "objective": f"Inspect the {title} fixture."}}) for title in ("broken", "healthy")]
        restore = corrupt(broken)

        # The older task cannot be read: it is skipped, and the other gets the decision.
        intent = await assignment_decision(hub, run["id"])
        assert intent["source_task_id"] == healthy["id"]
        assert [item["worker_id"] for item in intent["candidates"]["workers"]] == [idle["id"]]
        assert len(await skipped_events(hub, broken["id"])) == 1
        # The fault is reported once while it persists.
        assert await hub.next_assignment(run["id"]) is None
        assert len(await skipped_events(hub, broken["id"])) == 1

        restore()
        decided = await hub.complete_assignment(intent["id"], None)
        assert decided["outcome"] == "new_worker" and decided["assignment"]["task_id"] == healthy["id"]
        restored = await assignment_decision(hub, run["id"])
        assert restored["source_task_id"] == broken["id"]
        assert [item["worker_id"] for item in restored["candidates"]["workers"]] == [idle["id"]]
    finally:
        await hub.close()
