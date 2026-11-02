"""Recovery checks a unique owned cgroup instead of trusting lease expiry."""
from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

from formal_geyser import cgroups
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.ownership import planned_receipt, tree_quiet
from formal_geyser.coordination.runtime.process import OwnedProcessHost
from tests.coordination.test_fault_isolation_policy import execute
from tests.coordination.test_fault_isolation_policy import peers as peers


async def test_receipt_cannot_claim_a_live_tree_is_quiet(tmp_path):
    workspace = tmp_path / ".task-based/workers/example/work"
    workspace.mkdir(parents=True)
    host = await OwnedProcessHost.start([sys.executable, "-c", "import sys; sys.stdin.read()"], cwd=workspace)
    receipt = host.ownership_receipt
    try:
        assert receipt and not tree_quiet(receipt)
        assert not tree_quiet({**receipt, "process_quiet": True})
        assert not tree_quiet(receipt, end_orphan=True)  # Its owner, this process, is alive.
        await host.stop(grace=0.1)
        assert tree_quiet(receipt)
    finally:
        await host.stop(grace=0.1)
        await asyncio.gather(host.stdout.read(), host.stderr.read())


async def test_startup_automatically_recovers_only_after_the_original_tree_stops(peers, monkeypatch):
    hub, run, (attempt, unlaunched) = peers
    host = await OwnedProcessHost.start([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                       cwd=hub.workspace(attempt["worker_id"]))
    await execute(hub, "worker.bind_session", {"worker_id": attempt["worker_id"],
        "thread_id": "retained-original-thread", "model": "test"}, runner=True)
    await hub.note_owned_process("worker", attempt["id"], host.ownership_receipt)
    intent = await execute(hub, "turn.reserve", {"worker_id": attempt["worker_id"]}, runner=True)
    await hub.close()
    reopened = Hub(hub.root)
    try:
        assert await reopened.recover_quiet_runtime_failures(run["id"]) == []
        assert (await reopened.worker(attempt["worker_id"]))["active_attempt"] == attempt["id"]
        await host.stop(grace=0.1)
        # The current handler may still be saving its result after process exit.
        assert await reopened.recover_quiet_runtime_failures(run["id"], exclude_attempts={attempt["id"]}) == []
        assert (await reopened.worker(attempt["worker_id"]))["active_attempt"] == attempt["id"]
        await reopened.recover_quiet_runtime_failures(run["id"])
        assert (await reopened.worker(attempt["worker_id"]))["active_attempt"] is None
        now = reopened.clock()
        monkeypatch.setattr(reopened, "clock", lambda: now + 61000)
        # The peer's claim never reached a launch, so it is released without a probe.
        assert sorted(await reopened.recover_quiet_runtime_failures(run["id"])) == sorted(
            [attempt["worker_id"], unlaunched["worker_id"]])
        assert (await reopened.worker(attempt["worker_id"]))["thread_id"] == "retained-original-thread"
        retained = await reopened.db.call(lambda c: reopened._get(c, "turn_intents", intent["id"]))
        assert retained["state"] == "closed_unknown"
    finally:
        await host.stop(grace=0.1)
        await asyncio.gather(host.stdout.read(), host.stderr.read())
        await reopened.close()


async def test_claim_that_never_launched_is_released_after_restart(peers, monkeypatch):
    hub, run, (attempt, launched) = peers
    # The other claim reached its launch without a cgroup that recovery could probe.
    await hub.note_launch("worker", launched["id"], None)
    await hub.close()
    reopened = Hub(hub.root)
    try:
        assert (await reopened.worker(attempt["worker_id"]))["state"] == "recovery_required"
        await reopened.recover_quiet_runtime_failures(run["id"])
        assert (await reopened.worker(attempt["worker_id"]))["active_attempt"] is None
        assert (await reopened.worker(launched["worker_id"]))["active_attempt"] == launched["id"]
        now = reopened.clock()
        monkeypatch.setattr(reopened, "clock", lambda: now + 61000)
        assert await reopened.recover_quiet_runtime_failures(run["id"]) == [attempt["worker_id"]]
    finally:
        await reopened.close()


async def test_launch_without_ownership_record_waits_for_its_planned_tree(peers):
    hub, run, (attempt, _) = peers
    cgroup = await asyncio.to_thread(cgroups.root) / ("tree-" + uuid4().hex)
    planned = planned_receipt(cgroup)
    await hub.note_launch("worker", attempt["id"], planned)
    host = await OwnedProcessHost.start([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                       cwd=hub.workspace(attempt["worker_id"]), cgroup=cgroup)
    await hub.close()
    reopened = Hub(hub.root)
    try:
        assert host.ownership_receipt["cgroup"] == planned["cgroup"]
        await reopened.recover_quiet_runtime_failures(run["id"])
        assert (await reopened.worker(attempt["worker_id"]))["active_attempt"] == attempt["id"]
        await host.stop(grace=0.1)
        await reopened.recover_quiet_runtime_failures(run["id"])
        assert (await reopened.worker(attempt["worker_id"]))["active_attempt"] is None
    finally:
        await host.stop(grace=0.1)
        await asyncio.gather(host.stdout.read(), host.stderr.read())
        await reopened.close()


async def test_live_claim_of_this_coordinator_is_never_released(peers):
    hub, run, (attempt, _) = peers
    await hub.recover_quiet_runtime_failures(run["id"])
    assert (await hub.worker(attempt["worker_id"]))["active_attempt"] == attempt["id"]


async def test_runner_records_the_launch_before_any_process_opens(peers):
    hub, run, (attempt, _) = peers
    launches_at_open = []

    class Recording(ScriptedHarness):
        async def open(self, **kwargs):
            launches_at_open.append(await hub.db.call(lambda c: c.execute(
                "SELECT count(*) FROM events WHERE kind='execution.launch_started' AND object_id=?",
                (attempt["id"],)).fetchone()[0]))
            return await super().open(**kwargs)

    assignment = await hub.db.call(lambda c: hub._assignment_metadata(c, attempt))
    await WorkerRunner(hub, lambda: Recording([ScriptedTurn([])])).run_once(run["id"], assignment=assignment)
    assert launches_at_open == [1]
