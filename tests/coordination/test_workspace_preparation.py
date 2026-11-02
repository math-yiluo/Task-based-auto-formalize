"""Slow filesystem work cannot hold SQLite; stale ownership cannot commit it."""
import asyncio
import threading
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, Role
from tests.coordination.publication_helpers import new_hub
from tests.coordination.test_interruptions import interrupted

OP = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)


async def command(hub, name, payload=None, actor=OP):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


async def assigned(hub):
    task = await command(hub, "task.publish", {"contract": {
        "title": "Preparation fixture", "objective": "Write fixture files."}})
    run = await command(hub, "run.start")
    worker = await command(hub, "worker.register", {}, RUNNER)
    attempt = await command(hub, "task.claim", {"task_id": task["id"], "revision": 1,
                            "run_id": run["id"], "worker_id": worker["id"]}, RUNNER)
    return task, run, worker, attempt


async def test_snapshot_preparation_does_not_block_writes_and_rechecks_fence(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path / "project")
    entered, release = threading.Event(), threading.Event()
    try:
        task, run, worker, attempt = await assigned(hub)
        original = hub.blobs.snapshot
        def slow_snapshot(*args, **kwargs):
            entered.set()
            assert release.wait(15)
            return original(*args, **kwargs)
        monkeypatch.setattr(hub.blobs, "snapshot", slow_snapshot)
        pending = asyncio.create_task(command(hub, "runtime.abandon", {
            "worker_id": worker["id"], "quiescent": True, "reason": "Synthetic stopped process."}, RUNNER))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            # A real writer command, not just a WAL read, must complete meanwhile.
            await asyncio.wait_for(command(hub, "run.pause", {"run_id": run["id"]}), 3)
            await hub.db.write(lambda c: c.execute(
                "UPDATE workers SET usage_epoch=usage_epoch+1 WHERE id=?", (worker["id"],)))
        finally:
            release.set()
        with pytest.raises(HubError, match="Execution changed"):
            await pending
        assert (await hub.worker(worker["id"]))["active_attempt"] == attempt["id"]
    finally:
        release.set()
        await hub.close()


async def test_interruption_admission_prepares_snapshot_off_writer(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path / "project")
    try:
        run, task, worker, attempt, payload = await interrupted(hub)
        await command(hub, "runtime.park_interrupted", payload, RUNNER)
        await command(hub, "run.resume", {"run_id": run["id"]})
        original = hub.blobs.snapshot
        threads = []
        def capture(*args, **kwargs):
            threads.append(threading.current_thread().name)
            assert not threads[-1].startswith("taskhub-db")
            return original(*args, **kwargs)
        monkeypatch.setattr(hub.blobs, "snapshot", capture)
        resumed = await hub.next_assignment(run["id"])
        assert resumed["worker_id"] == worker["id"] and threads
    finally:
        await hub.close()


async def test_a_reviewer_reads_the_published_library_until_its_first_check(tmp_path):
    from tests.coordination.publication_helpers import file_payload
    from tests.coordination.test_collaboration_boundaries import (
        claim,
        machine_accept,
        peer_accept,
        publish,
        start,
    )

    hub = await new_hub(tmp_path / "project")
    try:
        host = await publish(hub, "Producer")
        run = await start(hub)
        author = await claim(hub, host, run)
        candidates = []
        for part in ("One", "Two"):
            candidates.append(await command(hub, "artifact.stage", file_payload(
                hub, author, kind="record", data={"part": part}), author))
            await machine_accept(hub, candidates[-1])
            if part == "One":
                await peer_accept(hub, candidates[-1], run)  # Published: the Library has files.
        reviewer = await claim(hub, await hub.task(candidates[-1]["review_task_id"]), run)

        def checkout(worker_id):
            work = hub.workspace(worker_id)
            return {path.relative_to(work).as_posix() for path in work.rglob("*")
                    if path.is_file() and ".private" not in path.relative_to(work).parts}
        library = checkout(author.worker_id)
        assert library and checkout(reviewer.worker_id) == set()
        assert await hub.db.read(lambda c: c.execute("SELECT 1 FROM attempt_workspaces WHERE attempt_id=?",
                                                     (reviewer.attempt_id,)).fetchone())  # Admitted all the same.
        assert await hub.ensure_library_copy(reviewer.worker_id) is not None
        assert checkout(reviewer.worker_id) and checkout(reviewer.worker_id) <= library
        assert await hub.ensure_library_copy(reviewer.worker_id) is None  # Written once.
        assert await hub.ensure_library_copy(author.worker_id) is None
    finally:
        await hub.close()


async def test_checkout_does_not_block_writes_and_precedes_assignment_delivery(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path / "project")
    entered, release = threading.Event(), threading.Event()
    pending = None
    try:
        hub.config.repository.enabled = True
        await command(hub, "repository.initialize", {})
        task = await command(hub, "task.publish", {"contract": {
            "title": "Checkout fixture", "objective": "Inspect the Library."}})
        run = await command(hub, "run.start")
        worker = await command(hub, "worker.register", {}, RUNNER)
        original = hub._refresh_repository_files
        def checkout(*args):
            assert not threading.current_thread().name.startswith("taskhub-db")
            entered.set()
            assert release.wait(15)
            return original(*args)
        monkeypatch.setattr(hub, "_refresh_repository_files", checkout)
        pending = asyncio.create_task(command(hub, "task.claim", {
            "task_id": task["id"], "revision": 1, "run_id": run["id"], "worker_id": worker["id"]}, RUNNER))
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(HubError, match="Finish the reserved workspace"):
            await command(hub, "turn.reserve", {"worker_id": worker["id"]}, RUNNER)
        await asyncio.wait_for(command(hub, "run.pause", {"run_id": run["id"]}), 3)
        assert not pending.done()
        assert await hub.db.read(lambda c: c.execute("SELECT count(*) FROM turn_intents").fetchone()[0]) == 0
        release.set()
        result = await pending
        assert result["worker_id"] == worker["id"]
        def unexpected_checkout(*args):
            pytest.fail("An exact claim replay must not refresh the already prepared workspace")
        monkeypatch.setattr(hub, "_refresh_repository_files", unexpected_checkout)
        assert await hub.prepare_assignment_workspace(result) == result
        assert await hub.db.read(lambda c: c.execute("SELECT count(*) FROM repository_bases").fetchone()[0]) == 1
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await hub.close()
