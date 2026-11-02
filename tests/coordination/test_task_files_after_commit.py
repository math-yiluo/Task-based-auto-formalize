"""A command's new task revision files are written after its transaction commits, off its thread."""
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.content import NativeContentStore
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role

OPERATOR = Actor(role=Role.OPERATOR)
CONTRACT = {"title": "Deferred files", "objective": "Read the exact task files.", "context": "Background."}


@pytest.fixture
async def hub(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        yield hub
    finally:
        await hub.close()


async def publish(hub):
    return await hub.execute(Command(name="task.publish", key=uuid4().hex, payload={"contract": CONTRACT}), OPERATOR)


async def pending(hub):
    return await hub.db.read(lambda c: [tuple(row) for row in c.execute("SELECT task_id,revision FROM task_tree_intents")])


async def test_task_files_are_written_after_commit_off_the_coordination_thread(hub, monkeypatch):
    threads = []
    original = NativeContentStore.task_files

    def observed(self, *args, **kwargs):
        threads.append(threading.current_thread().name)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(NativeContentStore, "task_files", observed)
    task = await publish(hub)
    assert threads and not any(name.startswith("taskhub-db") for name in threads)
    opened = await hub.task(task["id"])
    root = Path(opened["files"]["root"])
    assert (root / "objective.md").read_text(encoding="utf-8") == CONTRACT["objective"]
    assert task["files"] == opened["files"]  # The response named the files written before it returned.
    assert await pending(hub) == []
    assert await hub.db.read(lambda c: c.execute("SELECT count(*) FROM native_task_files").fetchone()[0]) == 1


async def test_reads_use_the_ledger_contract_until_a_failed_write_is_retried(hub, monkeypatch):
    original = NativeContentStore.task_files

    def unavailable(self, *args, **kwargs):
        raise OSError("Synthetic storage interruption")
    monkeypatch.setattr(NativeContentStore, "task_files", unavailable)
    task = await publish(hub)
    assert await pending(hub) == [(task["id"], 1)]
    read = await hub.task(task["id"])
    assert read["contract"]["objective"] == CONTRACT["objective"]
    assert not Path(read["files"]["root"]).exists()
    failures = await hub.db.read(lambda c: c.execute(
        "SELECT count(*) FROM events WHERE kind='task.files_failed' AND object_id=?", (task["id"],)).fetchone()[0])
    assert failures == 1
    monkeypatch.setattr(NativeContentStore, "task_files", original)
    assert await hub.reconcile_task_trees() == [(task["id"], 1)]
    assert await pending(hub) == []
    reread = await hub.task(task["id"])
    assert reread["contract"] == read["contract"] and reread["files"] == read["files"]
    assert (Path(reread["files"]["root"]) / "task.json").is_file()
