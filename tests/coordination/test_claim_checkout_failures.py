"""A claim whose checkout fails after its transaction committed never stays running.

No caller receives such a claim, so nothing would start or end it: in the Linux pilot one
waited two hours for a restart (attempt 9b4c0a41, then eacfc196), each time because the
Library repository lock stayed busy for its 30 seconds while the Integrator pruned an old
integration checkout.
"""
import asyncio
import json
import sqlite3
import threading
from uuid import uuid4

import portalocker
import pytest

from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.coordination.runtime import WorkerRunner
from tests.coordination.publication_helpers import new_hub

OPERATOR = Actor(role=Role.OPERATOR)


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


async def open_task(hub, title="Busy Library"):
    task = await command(hub, "task.publish", {"contract": {"title": title, "objective": "Read the Library."}})
    return task, await command(hub, "run.start")


async def claims(hub, task_id):
    return await hub.db.read(lambda c: [dict(row) for row in c.execute(
        "SELECT a.id,a.state,a.ended_at,a.worker_id,w.active_attempt,w.state AS worker_state "
        "FROM attempts a JOIN workers w ON w.id=a.worker_id WHERE a.task_id=? ORDER BY a.created_at", (task_id,))])


async def admission_failures(hub, worker_id):
    return await hub.db.read(lambda c: [json.loads(row[0]) for row in c.execute(
        "SELECT payload_json FROM events WHERE kind='worker.admission_failed' AND object_id=?", (worker_id,))])


async def test_a_lock_timeout_after_the_claim_commits_releases_the_claim(tmp_path, monkeypatch):
    now = [1_800_000_000_000]
    hub = await new_hub(tmp_path / "project", clock=lambda: now[0])
    try:
        task, run = await open_task(hub)
        original = hub._refresh_repository_files

        def busy(*args):
            # What portalocker raises when its 30-second wait ends: no OSError or HubError.
            raise portalocker.AlreadyLocked(BlockingIOError(11, "Resource temporarily unavailable"))
        monkeypatch.setattr(hub, "_refresh_repository_files", busy)
        assert await asyncio.wait_for(hub.next_assignment(run["id"]), 30) is None
        [claim] = await claims(hub, task["id"])  # The claim had committed before the checkout.
        assert claim["state"] == "preparation_failed" and claim["ended_at"] is not None
        assert claim["active_attempt"] is None and claim["worker_state"] == "recovery_required"
        assert await hub.db.read(lambda c: c.execute("SELECT count(*) FROM turn_intents").fetchone()[0]) == 0
        [failure] = await admission_failures(hub, claim["worker_id"])
        assert failure["attempt_admitted"] is False and "AlreadyLocked" in failure["reason"]

        # As after any failed checkout, the worker is admitted again once its retry is due.
        monkeypatch.setattr(hub, "_refresh_repository_files", original)
        now[0] += 31_000
        await hub.recover_quiet_runtime_failures(run["id"])
        assignment = await hub.next_assignment(run["id"])
        assert assignment["task_id"] == task["id"] and assignment["worker_id"] == claim["worker_id"]
        assert (await hub.worker(claim["worker_id"]))["active_attempt"] == assignment["id"]
    finally:
        await hub.close()


async def test_a_checkout_behind_a_held_repository_lock_releases_its_claim(tmp_path, monkeypatch):
    from formal_geyser.coordination import repository as repository_module
    monkeypatch.setattr(repository_module, "LOCK_SECONDS", 0.3)
    hub = await new_hub(tmp_path / "project")
    held, release = threading.Event(), threading.Event()

    def holder():
        # Every _repository() call makes its own store, with its own in-process lock: like the
        # Integrator pruning, this one meets the checkout only at the file lock.
        with hub._repository()._locked():
            held.set()
            release.wait(30)
    thread = threading.Thread(target=holder)
    try:
        task, run = await open_task(hub)
        repository_module._SNAPSHOTS.clear()
        thread.start()
        assert held.wait(10)
        assert await asyncio.wait_for(hub.next_assignment(run["id"]), 30) is None
        [claim] = await claims(hub, task["id"])
        assert claim["state"] == "preparation_failed" and claim["active_attempt"] is None
        [failure] = await admission_failures(hub, claim["worker_id"])
        assert "Library repository stayed locked" in failure["reason"]
    finally:
        release.set()
        if thread.is_alive():
            thread.join()
        await hub.close()


async def test_a_cancelled_checkout_releases_its_claim(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path / "project")
    entered, release = threading.Event(), threading.Event()
    pending = None
    try:
        task, run = await open_task(hub)
        original = hub._refresh_repository_files

        def slow(*args):
            entered.set()
            assert release.wait(15)
            return original(*args)
        monkeypatch.setattr(hub, "_refresh_repository_files", slow)
        pending = asyncio.create_task(hub.next_assignment(run["id"]))
        assert await asyncio.to_thread(entered.wait, 10)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        [claim] = await claims(hub, task["id"])
        assert claim["state"] == "preparation_failed" and claim["active_attempt"] is None
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await hub.close()


async def test_a_handed_over_claim_whose_control_check_fails_is_held(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path / "project")
    try:
        task, run = await open_task(hub)
        assignment = await hub.next_assignment(run["id"])

        async def unreadable(*args, **kwargs):
            raise sqlite3.OperationalError("Synthetic unreadable run control")
        monkeypatch.setattr(hub, "run", unreadable)
        runner = WorkerRunner(hub, lambda: pytest.fail("No native session starts"))
        with pytest.raises(sqlite3.OperationalError):
            await runner.run_once(run["id"], assignment=assignment)
        [claim] = await claims(hub, task["id"])
        assert claim["state"] == "preflight_held" and claim["active_attempt"] is None
        assert not runner._executing
    finally:
        await hub.close()


async def test_a_claim_command_whose_checkout_fails_is_released(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path / "project")
    try:
        task, run = await open_task(hub)
        worker = await command(hub, "worker.register", {}, Actor(role=Role.RUNNER))

        def broken(*args):
            raise RuntimeError("Synthetic checkout defect")
        monkeypatch.setattr(hub, "_refresh_repository_files", broken)
        with pytest.raises(RuntimeError, match="Synthetic checkout defect"):
            await command(hub, "task.claim", {"task_id": task["id"], "revision": 1, "run_id": run["id"],
                                              "worker_id": worker["id"]}, Actor(role=Role.RUNNER))
        [claim] = await claims(hub, task["id"])
        assert claim["state"] == "preparation_failed" and claim["active_attempt"] is None
    finally:
        await hub.close()
