"""Model-free test drivers for the real one-shot choice and dispatch protocols."""
from uuid import uuid4

from formal_geyser.coordination.models import Actor, Command, Role


async def _command(hub, name, payload):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload), Actor(role=Role.OPERATOR))


async def start_helper(hub, intent_id, thread_id):
    """What the runner reports once a helper's conversation exists."""
    return await hub.execute(Command(name="helper.started", key=uuid4().hex,
                                     payload={"intent_id": intent_id, "thread_id": thread_id}), Actor(role=Role.RUNNER))


async def idle_worker(hub, run, title="Earlier task"):
    """A worker that executed a task titled `title` and left it: idle, with that task in its history."""
    task = await _command(hub, "task.publish", {"contract": {"title": title, "objective": f"Work on {title}."}})
    worker = await _command(hub, "worker.register", {})
    attempt = await _command(hub, "task.claim", {"task_id": task["id"], "revision": task["revision"],
                                                 "run_id": run["id"], "worker_id": worker["id"]})
    await hub.execute(Command(name="worker.bind_session", key=uuid4().hex, payload={
        "worker_id": worker["id"], "thread_id": "thread-" + uuid4().hex,
        "codex_profile": hub.config.codex.model_dump(mode="json")}), Actor(role=Role.RUNNER))
    await hub.ordinary_turn_completed(worker["id"], attempt["id"])
    await _command(hub, "task.leave", {"task_id": task["id"], "worker_id": worker["id"], "permanent": True,
                                       "reason": "The synthetic executor finished its part and leaves."})
    return await hub.worker(worker["id"])


async def assignment_decision(hub, run_id):
    """The one prepared assignment decision (next_assignment creates it when idle workers exist)."""
    assert await hub.next_assignment(run_id) is None, "Expected the open task to wait for a decision"
    [intent] = await hub.due_assignment_intents(run_id)
    return intent


async def open_collaboration(hub, task_id, *, invitation="Independently verify the remaining conditions."):
    """Supply a deterministic first-turn choice without invoking a model.

    Low-level Hub tests have no running native process. They may keep an attempt
    open across this synthetic native-turn boundary to test fencing directly.
    Runtime tests should drive the corresponding helper through their harness.
    """
    task = await hub.task(task_id)
    lifecycle = task["collaboration"].get("recruitment")
    if lifecycle is None:
        lifecycle = await hub.db.call(lambda c: hub._lifecycle(c, task_id), write=True)
    if lifecycle["opportunity_state"] == "open":
        return
    initial = lifecycle["initial_worker_id"]
    if initial is None:
        raise AssertionError("An initial executor must exist before its recruitment choice")
    intent = await hub.record_first_turn(initial, task_id, "synthetic-first-turn-" + uuid4().hex)
    if intent is None:
        raise AssertionError("The recruitment opportunity cannot be reopened")
    await hub.decide_recruitment(intent["id"], True, invitation)


async def validate_candidate(hub, candidate_id, *, verdict="accepted"):
    """Record the machine validation verdict the runner's validator gives a staged candidate."""
    binding = await hub.db.call(lambda c: hub._candidate_binding(c, candidate_id))
    return await hub.execute(Command(name="artifact.accept", key=uuid4().hex, payload={
        "artifact_id": candidate_id, "policy": binding["policy"], "verdict": verdict,
        "details": {"scope": "synthetic machine validation"}, "qualifications": []}),
        Actor(role=Role.VALIDATOR))


async def select_review(hub, review_task_id, run_id):
    """Dispatch an open review to its own new reviewer, as admission does, and return the attempt.

    A review starts only after machine validation accepted its exact candidate. A candidate
    staged without any validation first receives the accepting verdict the runner records
    right after staging; any other recorded verdict is left for the test to resolve.
    Reviews are never selected by, or forked from, an ordinary worker.
    """
    request = await hub.db.call(lambda c: dict(c.execute(
        "SELECT r.candidate_id,b.policy FROM review_requests r JOIN candidate_bindings b "
        "ON b.artifact_id=r.candidate_id WHERE r.task_id=?", (review_task_id,)).fetchone()))
    if not await hub.db.call(lambda c: c.execute("SELECT 1 FROM evidence WHERE artifact_id=? AND policy=?",
                                                 (request["candidate_id"], request["policy"])).fetchone()):
        await validate_candidate(hub, request["candidate_id"])

    def dispatch(c):
        candidate = hub._open_candidate(c, review_task_id)
        if candidate is None or candidate["opportunity"] != "review":
            raise AssertionError("The review is unavailable for a new reviewer")
        return hub._fresh_review(c, candidate, run_id)
    result = await hub.db.call(dispatch, write=True)
    return await hub.prepare_assignment_workspace(result, tolerate_failure=True)


async def resolve_recruitment(hub, run_id):
    """Finish pending synthetic first-turn choices without starting models."""
    for intent in await hub.due_recruitment_intents(run_id):
        await hub.decide_recruitment(intent["id"], False, "")


async def finish_ordinary(hub, task_id, declarer, *, artifact_ids, feedback_worker=None):
    """Declare, resolve one feedback opportunity, and decide with exact results."""
    task = await hub.task(task_id)
    conditions = await hub.db.call(lambda c: hub._conditions(c, hub._task(c, task_id)))
    declaration = await hub.execute(Command(name="task.complete.declare", key=uuid4().hex, payload={
        "task_id": task_id, "revision": task["revision"],
        "evidence": {key: {"artifact_ids": artifact_ids, "explanation": "The exact accepted result satisfies: " + value}
                     for key, value in conditions.items()},
    }), declarer)
    if feedback_worker:
        await hub.execute(Command(name="task.complete.feedback", key=uuid4().hex, payload={
            "declaration_id": declaration["id"], "conditions": [],
            "checkpoint": {"summary": "Saved collaborator work.", "next_action": "Select another suitable task."},
        }), feedback_worker)
    return await hub.execute(Command(name="task.complete.finalize", key=uuid4().hex, payload={
        "declaration_id": declaration["id"], "reason": "All original acceptance conditions have exact accepted and published evidence.",
    }), declarer)


async def record_saved_turn(hub, worker_id):
    """Mark the worker's bound test thread as an existing conversation: Codex saves a thread
    only after a turn, and turns count only on the thread they ran on."""
    def insert(c):
        worker = c.execute("SELECT run_id,thread_id FROM workers WHERE id=?", (worker_id,)).fetchone()
        c.execute("INSERT INTO turn_intents(id,worker_id,attempt_id,run_id,remote_turn_id,state,created_at,thread_id) "
                  "VALUES(?,?,?,?,NULL,'completed',?,?)",
                  (uuid4().hex, worker_id, "saved-attempt", worker["run_id"] or "saved-run", hub.clock(),
                   worker["thread_id"]))
    await hub.db.call(insert, write=True)
