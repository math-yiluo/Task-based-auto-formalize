"""One unreadable task or worker cannot stop admission of unrelated work."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from formal_geyser.coordination.models import Actor, Command, Role
from tests.coordination.publication_helpers import new_hub

OPERATOR = Actor(role=Role.OPERATOR)


async def command(hub, name, payload=None, actor=OPERATOR, key=None):
    return await hub.execute(Command(name=name, key=key or uuid4().hex, payload=payload or {}), actor)


async def publish(hub, title):
    return await command(hub, "task.publish", {"contract": {
        "title": title, "objective": f"Inspect the {title} fixture."}})


async def skipped_events(hub, task_id):
    return await hub.db.call(lambda c: c.execute(
        "SELECT payload_json FROM events WHERE kind='admission.item_skipped' AND object_id=?",
        (task_id,)).fetchall())


async def test_corrupted_task_file_is_skipped_and_reported_once(tmp_path):
    hub = await new_hub(tmp_path / "project")
    try:
        healthy = await publish(hub, "root")
        broken = await publish(hub, "second")
        task_file = Path(broken["files"]["path"])
        task_file.chmod(0o644)
        task_file.write_bytes(task_file.read_bytes() + b" ")
        run = await command(hub, "run.start")

        assignment = await hub.next_assignment(run["id"])
        assert assignment is not None and assignment["task_id"] == healthy["id"]
        assert len(await skipped_events(hub, broken["id"])) == 1

        # Later passes keep skipping the same fault without repeating the report.
        await hub.next_assignment(run["id"])
        assert len(await skipped_events(hub, broken["id"])) == 1
    finally:
        await hub.close()


async def test_runtime_usage_samples_leave_no_receipts(tmp_path):
    hub = await new_hub(tmp_path / "project")
    try:
        await publish(hub, "usage")
        run = await command(hub, "run.start")
        attempt = await hub.next_assignment(run["id"])
        runner = Actor(role=Role.RUNNER)
        for total in (10, 20, 30):
            await command(hub, "usage.record", {"worker_id": attempt["worker_id"],
                          "sample": {"totalTokens": total}}, runner, key="runtime:" + uuid4().hex)
        count = await hub.db.call(lambda c: c.execute(
            "SELECT count(*) FROM receipts WHERE command='usage.record'").fetchone()[0])
        assert count == 0
        # Explicitly keyed commands keep their idempotency receipts.
        await command(hub, "usage.record", {"worker_id": attempt["worker_id"],
                      "sample": {"totalTokens": 40}}, runner, key="operator-retry")
        count = await hub.db.call(lambda c: c.execute(
            "SELECT count(*) FROM receipts WHERE command='usage.record'").fetchone()[0])
        assert count == 1
    finally:
        await hub.close()
