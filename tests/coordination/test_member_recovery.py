from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    CodexConfig,
    Command,
    ProjectConfig,
    Role,
)
from tests.coordination.lifecycle_helpers import open_collaboration

OP = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)


async def command(hub, name, payload=None, actor=OP):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


@pytest.fixture
async def hub(tmp_path):
    initialize(tmp_path, ProjectConfig(codex=CodexConfig(home=str(tmp_path / "codex-home"))))
    value = Hub(tmp_path)
    yield value
    await value.close()


async def test_both_reconciled_teammates_can_recover_after_task_becomes_ready(hub):
    task = await command(hub, "task.publish", {"contract": {"title": "Two members", "objective": "Preserve both workers."}})
    run = await command(hub, "run.start")
    workers = [await command(hub, "worker.register") for _ in range(2)]
    await command(hub, "task.claim", {"task_id": task["id"], "revision": 1,
                                      "worker_id": workers[0]["id"], "run_id": run["id"]}, RUNNER)
    await open_collaboration(hub, task["id"])
    await command(hub, "task.join", {"task_id": task["id"], "revision": 1,
                                     "worker_id": workers[1]["id"], "run_id": run["id"],
                                     "plan": "Work on the second independent part."})
    await command(hub, "run.pause", {"run_id": run["id"]})
    for worker in workers:
        await command(hub, "runtime.abandon", {"worker_id": worker["id"], "quiescent": True}, RUNNER)
    # Exact terminal reconciliation separately establishes idle native sessions;
    # the durable membership recovery hold must survive aggregate state changes.
    await hub.db.call(lambda c: c.execute("UPDATE workers SET state='idle'"), write=True)
    for worker in workers:
        result = await command(hub, "worker.recover", {"worker_id": worker["id"],
                               "process_confirmed_stopped": True, "reason": "Verified terminal and stopped processes."})
        assert result["directive"] == "recovered"
        assert (await hub.task(task["id"]))["state"] == "ready"
    members = await hub.db.call(lambda c: [dict(row) for row in c.execute(
        "SELECT * FROM task_members WHERE task_id=?", (task["id"],))])
    assert len(members) == 2 and all(row["runnable"] == 1 for row in members)
    with pytest.raises(HubError):
        await command(hub, "worker.recover", {"worker_id": workers[1]["id"],
                      "process_confirmed_stopped": True, "reason": "Do not recover an already healthy member."})
