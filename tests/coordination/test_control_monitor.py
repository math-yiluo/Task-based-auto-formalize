"""Turns and validations under way share one control read each interval."""
from __future__ import annotations

import asyncio
import sqlite3
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.coordination.runtime.control_monitor import (
    ControlMonitor,
    ControlUnavailable,
)
from tests.coordination.publication_helpers import new_hub


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), Actor(role=Role.OPERATOR))


async def write(hub, sql, parameters=()):
    await hub.db.call(lambda c: c.execute(sql, parameters), write=True)


async def assert_alone(hub, watches):
    """Each watch's view is what run(), worker() and pending_message_signal() say on their own."""
    views = await hub.activation_controls(watches)
    for run_id, worker_id in watches:
        try:
            run = await hub.run(run_id, worker_id=worker_id)
            worker = await hub.worker(worker_id) if worker_id is not None else None
        except HubError:
            assert (run_id, worker_id) not in views
            continue
        assert views[run_id, worker_id] == {
            "run": {key: run[key] for key in ("state", "pause_mode", "stop_reason")},
            "worker": None if worker is None else {
                key: worker[key] for key in ("active_attempt", "state", "runtime_stop_reason")},
            "messages": worker_id is not None and await hub.pending_message_signal(worker_id),
            "human_messages": worker_id is not None and await hub.pending_message_signal(worker_id, human=True)}
    return views


async def test_one_read_gives_what_each_watch_reads_alone(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        await command(hub, "task.publish", {"contract": {"title": "Watch", "objective": "Keep working."}})
        run = await command(hub, "run.start")
        active = await hub.next_assignment(run["id"])
        others = [(await command(hub, "worker.register"))["id"] for _ in range(3)]
        workers = [active["worker_id"], *others]
        watches = [(run["id"], worker) for worker in workers] + [
            (run["id"], None), (run["id"], "nobody"), ("no-run", workers[0])]
        views = await assert_alone(hub, watches)
        assert views[run["id"], workers[0]]["worker"]["active_attempt"] == active["id"]
        await command(hub, "message.send", {"worker_id": workers[1], "message_id": "m", "text": "Read this."})
        await command(hub, "worker.pause", {"worker_id": workers[2]})
        await command(hub, "worker.pause_after_turn", {"worker_id": workers[3]})
        views = await assert_alone(hub, watches)
        assert views[run["id"], workers[1]]["messages"] and views[run["id"], workers[1]]["human_messages"]
        assert views[run["id"], workers[2]]["run"]["pause_mode"] == "now"
        assert views[run["id"], workers[3]]["run"]["pause_mode"] == "after_turn"
        await command(hub, "run.pause_after_turn", {"run_id": run["id"]})
        await command(hub, "worker.resume", {"worker_id": workers[1]})
        views = await assert_alone(hub, watches)
        assert views[run["id"], workers[1]]["run"]["state"] == "running"
        assert views[run["id"], None]["run"]["pause_mode"] == "after_turn"
        await command(hub, "run.pause", {"run_id": run["id"]})
        await write(hub, "INSERT INTO attempt_stop_requests VALUES(?,?,?)", (active["id"], "assignment_changed", 1))
        views = await assert_alone(hub, watches)
        assert views[run["id"], workers[0]]["worker"]["runtime_stop_reason"] == "assignment_changed"
        assert views[run["id"], None]["run"]["stop_reason"] == "operator_pause"
        await write(hub, "UPDATE project_lifecycle SET state='shutting_down' WHERE id=1")
        views = await assert_alone(hub, watches)
        assert views[run["id"], None]["run"]["stop_reason"] == "project_shutdown"
        await write(hub, "UPDATE project_lifecycle SET state='running' WHERE id=1")
        await command(hub, "run.stop", {"run_id": run["id"]})
        views = await assert_alone(hub, watches)
        assert views[run["id"], None]["run"]["stop_reason"] == "operator_stop"
    finally:
        await hub.close()


async def test_the_read_has_the_same_statements_for_any_number_of_watches(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path)
    try:
        run = await command(hub, "run.start")
        workers = [(await command(hub, "worker.register"))["id"] for _ in range(12)]
        statements: list[str] = []
        original = hub.db.read

        async def traced(function):
            def read(c):
                c.set_trace_callback(statements.append)
                try:
                    return function(c)
                finally:
                    c.set_trace_callback(None)
            return await original(read)
        monkeypatch.setattr(hub.db, "read", traced)
        await hub.activation_controls([(run["id"], workers[0])])
        alone = len(statements)
        statements.clear()
        await hub.activation_controls([(run["id"], worker) for worker in workers] + [(run["id"], None)])
        assert len(statements) == alone == 2
    finally:
        await hub.close()


class Controls:
    """A hub whose control reads are numbered, and can be held or fail."""

    def __init__(self):
        self.reads: list[set] = []
        self.gate: asyncio.Event | None = None
        self.failure: Exception | None = None

    async def activation_controls(self, watches):
        self.reads.append(set(watches))
        number = len(self.reads)
        if self.gate is not None:
            await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        return {watch: {"read": number} for watch in watches if watch[1] != "nobody"}


async def test_one_read_serves_every_watch_and_reading_stops_with_the_last():
    hub = Controls()
    monitor = ControlMonitor(hub, 0.01)
    async with (monitor.watch("run", "a") as first, monitor.watch("run", "b") as second,
                monitor.watch("run", "a") as again):
        views = [await watch.next() for watch in (first, second, again)]
        assert {("run", "a"), ("run", "b")} in hub.reads  # Both in one read; a watch twice is read once.
        later = await first.next()
        assert later["read"] > views[0]["read"]  # Each look comes from a later read.
        assert first.latest() == second.latest() == await again.next()
    async with asyncio.timeout(1):
        while not monitor._reading.done():
            await asyncio.sleep(0.01)
    reads = len(hub.reads)
    await asyncio.sleep(0.05)
    assert len(hub.reads) == reads  # Nothing watched, nothing read.


async def test_a_watch_only_sees_reads_begun_after_its_last_look():
    hub = Controls()
    monitor = ControlMonitor(hub, 0.01)
    async with monitor.watch("run", "a") as first:
        await first.next()
        hub.gate = asyncio.Event()
        count = len(hub.reads)
        async with asyncio.timeout(1):
            while len(hub.reads) == count:
                await asyncio.sleep(0.005)
        # A read is under way without the second watch; its result is not the second's view.
        async with monitor.watch("run", "b") as second:
            assert second.latest() is None
            waiting = asyncio.create_task(second.next())
            hub.gate.set()
            hub.gate = None
            view = await asyncio.wait_for(waiting, 1)
            assert view["read"] == count + 2 and ("run", "b") in hub.reads[view["read"] - 1]


async def test_an_unreadable_or_missing_control_is_reported_to_its_watchers():
    hub = Controls()
    monitor = ControlMonitor(hub, 0.01)
    async with monitor.watch("run", "nobody") as missing:
        with pytest.raises(ControlUnavailable, match="No run run or worker nobody"):
            await missing.next()
    hub.failure = sqlite3.OperationalError("Synthetic unreadable control")
    async with monitor.watch("run", "a") as watch:
        with pytest.raises(ControlUnavailable, match="OperationalError") as raised:
            await watch.next()
        assert raised.value.__cause__ is hub.failure
        with pytest.raises(ControlUnavailable):
            watch.latest()
        hub.failure = None
        assert (await watch.next())["read"] > 1  # The next read serves it again.
