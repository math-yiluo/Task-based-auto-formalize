"""Admission reads only what it may admit and leaves Library checkouts to the activation.

In the Linux pilot one selection ran about 1,100 statements, each slow on the busy coordination
thread, and every admission waited about 20 s for its worker's Library checkout."""
import asyncio

from formal_geyser.coordination.batch_integration import BATCH_DELAY_MS
from formal_geyser.coordination.runtime import ScriptedHarness, WorkerRunner
from formal_geyser.coordination.runtime import runner as runner_module
from tests.coordination.lifecycle_helpers import resolve_recruitment
from tests.coordination.test_collaborative_core import claim, command, machine, publish, stage
from tests.coordination.test_integrator_batches import batch_case as batch_case  # noqa: F401
from tests.coordination.test_integrator_batches import ready

OPEN = "SELECT id FROM tasks WHERE state NOT IN ('done','retired','draining','recovery_required')"


async def saved_work(hub, run, title, **values):
    """A worker that ended a normal turn on its task: it continues that task when admitted."""
    attempt = await claim(hub, await publish(hub, title, **values), run)
    await hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    await resolve_recruitment(hub, run["id"])  # The first turn's decision: no collaborator.
    return attempt


async def test_the_candidate_query_keeps_every_admissible_task_in_admission_order(batch_case):  # noqa: F811
    hub, run, _ = batch_case
    await ready(hub, run, "Accepted and waiting for integration")
    author = await claim(hub, await publish(hub, "Validated, waiting for a reviewer"), run)
    await machine(hub, await stage(hub, author))
    later = await saved_work(hub, run, "Saved work, default priority")
    urgent = await saved_work(hub, run, "Saved work, created later but urgent", priority=0)
    await publish(hub, "Published, nobody on it yet")

    def compare(c):
        result = {}
        for reviews in (True, False):
            candidates = hub._admission_candidates(c, reviews=reviews)
            exact = [row[0] for row in c.execute(OPEN) if hub._saved_work_possible(c, row[0])
                     and bool(c.execute("SELECT 1 FROM review_requests WHERE task_id=?", (row[0],)).fetchone()) == reviews]
            assert set(exact) <= set(candidates)
            result[reviews] = candidates
        return result
    candidates = await hub.db.call(compare)
    assert candidates[True], "the validated candidate's review must be offered"
    others = candidates[False]
    assert others.index(urgent["task_id"]) < others.index(later["task_id"])
    # An account that cannot start a session leaves its workers' saved work out.
    backend = (await hub.worker(later["worker_id"]))["backend"]
    blocked = await hub.db.call(lambda c: hub._admission_candidates(c, reviews=False, unstartable={backend}))
    assert later["task_id"] not in blocked and urgent["task_id"] not in blocked


async def test_only_the_admitted_task_is_read_in_full(batch_case, monkeypatch):  # noqa: F811
    hub, run, _ = batch_case
    attempts = [await saved_work(hub, run, f"Saved work {number}") for number in range(3)]
    read = []
    original = hub._task

    def counted(c, task_id, *args, **kwargs):
        read.append(task_id)
        return original(c, task_id, *args, **kwargs)
    monkeypatch.setattr(hub, "_task", counted)
    monkeypatch.setattr(hub, "_spacing_blocked", lambda c: set())  # The service has only just started.
    assignment = await hub.next_assignment(run["id"])
    # Equal priority and (with this fixture's fixed clock) equal age: the task ID decides.
    first, *others = sorted(attempt["task_id"] for attempt in attempts)
    assert assignment["task_id"] == first
    assert not set(others) & set(read)


async def test_admission_can_leave_the_library_checkout_to_the_activation(batch_case):  # noqa: F811
    hub, run, now = batch_case
    await ready(hub, run, "Accepted contribution")
    now[0] += BATCH_DELAY_MS
    claimed = await hub.next_assignment(run["id"], prepare=False)

    def checkout(c):
        return c.execute("SELECT 1 FROM attempt_workspaces WHERE attempt_id=?", (claimed["id"],)).fetchone()
    assert await hub.db.read(checkout) is None
    assert await hub.prepare_assignment_workspace(claimed, tolerate_failure=True) == claimed
    assert await hub.db.read(checkout) is not None


async def test_the_activation_prepares_the_checkout_and_a_failed_one_starts_nothing(batch_case, monkeypatch):  # noqa: F811
    hub, run, _ = batch_case
    runner = WorkerRunner(hub, ScriptedHarness)
    started, prepared = [], []

    class Activation:
        def __init__(self, runner, run_id, assignment):
            self.assignment = assignment

        async def run(self):
            started.append(self.assignment["id"])
            return {"worker_id": self.assignment["worker_id"], "turn_status": "completed"}

    async def prepare(assignment, *, tolerate_failure=False):
        prepared.append(assignment["id"])
        await asyncio.sleep(0)
        return None if assignment["id"] == "broken" else assignment

    async def running(run_id, **_):
        return {"state": "running"}
    monkeypatch.setattr(runner_module, "Activation", Activation)
    monkeypatch.setattr(hub, "prepare_assignment_workspace", prepare)
    monkeypatch.setattr(hub, "run", running)
    assert await runner.run_once(run["id"], assignment={"id": "good", "worker_id": "w", "task_id": "t"})
    assert await runner.run_once(run["id"], assignment={"id": "broken", "worker_id": "w", "task_id": "t"}) is None
    assert prepared == ["good", "broken"] and started == ["good"]
    assert "broken" not in runner._executing


async def open_pool(hub, run, *, sessions, tasks=6, idle=8):
    """Open tasks nobody works on yet, idle workers, and one running Codex session."""
    await command(hub, "runtime.select_backend", {"backend": "codex"})
    await claim(hub, await publish(hub, "Running"), run)
    await command(hub, "runtime.set_concurrency", {"backend": "codex", "sessions": sessions})
    pool = [(await publish(hub, f"Open task {number}"))["id"] for number in range(tasks)]
    for _ in range(idle):
        await command(hub, "worker.register")
    return pool


async def admission_cost(hub, run, monkeypatch):
    """One admission pass: its result, its statements, and the open tasks it read in full."""
    read, statements = [], []
    original = hub._task

    def counted(c, task_id, *args, **kwargs):
        read.append(task_id)
        return original(c, task_id, *args, **kwargs)
    monkeypatch.setattr(hub, "_task", counted)
    await hub.db.call(lambda c: c.set_trace_callback(statements.append))
    try:
        result = await hub.next_assignment(run["id"], prepare=False)
    finally:
        await hub.db.call(lambda c: c.set_trace_callback(None))
        monkeypatch.setattr(hub, "_task", original)
    return result, len(statements), read


async def test_with_every_session_in_use_admission_reads_no_open_task(batch_case, monkeypatch):  # noqa: F811
    # In the pilot (Codex 45 of 45, 26 open tasks, 43 idle workers) each pass read every open
    # task in full to place nobody: about 800 statements, two to three seconds.
    hub, run, _ = batch_case
    await open_pool(hub, run, sessions=1)
    result, statements, read = await admission_cost(hub, run, monkeypatch)
    assert result is None and read == [] and statements < 60


async def test_while_a_decision_runs_admission_reads_no_task_that_waits_for_one(batch_case, monkeypatch):  # noqa: F811
    # With free sessions and idle workers, every pass during a decision read each open task in
    # full and checked each idle worker for each: about 6,100 statements for 26 tasks and 43 workers.
    hub, run, _ = batch_case
    pool = await open_pool(hub, run, sessions=100)
    monkeypatch.setattr(hub, "_spacing_blocked", lambda c: set())
    assert (await admission_cost(hub, run, monkeypatch))[0] is None  # It begins a decision.
    decisions = await hub.db.read(lambda c: [row[0] for row in c.execute(
        "SELECT source_task_id FROM helper_intents WHERE kind='assignment' AND state='prepared'")])
    assert len(decisions) == 1 and decisions[0] in pool
    result, statements, read = await admission_cost(hub, run, monkeypatch)
    assert result is None and read == [] and statements < 120


async def test_a_pass_during_a_decision_does_not_grow_with_the_open_tasks(batch_case, monkeypatch):  # noqa: F811
    # Each open task's decisions and executors were read one task at a time: about 1,000
    # statements a pass that admitted nothing with the pilot's 323 open tasks.
    hub, run, _ = batch_case
    await open_pool(hub, run, sessions=100, tasks=4)
    monkeypatch.setattr(hub, "_spacing_blocked", lambda c: set())
    assert (await admission_cost(hub, run, monkeypatch))[0] is None  # It begins a decision.
    _, before, _ = await admission_cost(hub, run, monkeypatch)
    for number in range(20):
        await publish(hub, f"Later open task {number}")
    result, after, read = await admission_cost(hub, run, monkeypatch)
    assert result is None and read == [] and after == before
