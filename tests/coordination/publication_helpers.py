"""File-backed fixtures using the actual repository and review command boundaries."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime import ScriptedHarness
from tests.coordination.gateway_helpers import file_arguments


class FileHarness(ScriptedHarness):
    """A scripted native worker that starts with explicit authored source files."""

    def __init__(self, turns=(), *, files):
        super().__init__(turns)
        self.files = files

    async def open(self, **kwargs):
        session = await super().open(**kwargs)
        for path, body in self.files.items():
            target = kwargs["workspace"] / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        turns = list(self.turns)
        for turn in turns:
            for action in turn.actions:
                if action.name == "taskhub_call":
                    action.arguments["arguments"] = file_arguments(
                        kwargs["workspace"], action.arguments["operation"], action.arguments["arguments"])
        self.turns = iter(turns)
        return session


async def _command(hub, name, payload=None, *, actor=None):
    return await hub.execute(
        Command(name=name, key=uuid4().hex, payload=payload or {}),
        actor or Actor(role=Role.OPERATOR),
    )


def _worker(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


async def new_hub(root, *, config=None, files=None, **options):
    """Initialize a real, recorded source repository before any worker is admitted."""
    config = config or ProjectConfig()
    config = config.model_copy(update={
        "repository": config.repository.model_copy(update={"enabled": True}),
    })
    initialize(root, config)
    hub = Hub(root, **options)
    try:
        snapshot = hub.blobs.put_json({"files": [
            {"path": path, "sha256": hub.blobs.put(body)}
            for path, body in sorted((files or {}).items())
        ]})
        await _command(hub, "repository.initialize", {"snapshot": snapshot})
    except BaseException:
        await hub.close()
        raise
    return hub


def file_payload(hub, attempt, *, data, kind="software.result", path=None, **fields):
    """Write the synthetic work product and select its exact repository path."""
    worker_id = attempt.worker_id if isinstance(attempt, Actor) else attempt["worker_id"]
    return source_payload(hub.workspace(worker_id), data=data, kind=kind, path=path, **fields)


def source_payload(workspace, *, data, kind="software.result", path=None, **fields):
    path = path or f"results/{uuid4().hex}.json"
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"kind": kind, "data": data, "repository_paths": [path], **fields}


async def stage_note(hub, *, data, kind="research.note"):
    """Create an actual authored note candidate for native storage/inspection tests."""
    task = await _command(hub, "task.publish", {"contract": {
        "title": "Inspect the native note fixture", "objective": "Retain the exact authored note file.",
        "acceptance_policy": "general.peer@1",
    }})
    run = await _command(hub, "run.start")
    worker = await _command(hub, "worker.register")
    attempt = await _command(hub, "task.claim", {
        "task_id": task["id"], "revision": task["revision"],
        "worker_id": worker["id"], "run_id": run["id"],
    })
    return await _command(hub, "artifact.stage", file_payload(
        hub, attempt, kind=kind, data=data), actor=_worker(attempt))


async def approve(hub, reviewer, payload=None):
    """Accept the review, end its turn, and publish it through the dedicated Integrator."""
    if isinstance(reviewer, Actor):
        attempt = await hub.authorize_worker(reviewer)
        reviewer = {**attempt, "task": await hub.task(attempt["task_id"])}
    binding = reviewer["task"]["collaboration"]["review"]
    if payload is None:
        payload = {key: binding[key] for key in (
            "candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")}
        payload.update(
            rationale="Compared the exact source files and task contract.",
            evidence=["Inspected the frozen work product before integrating its selected files."],
            qualifications=[],
        )
    await _command(hub, "review.accept", payload, actor=_worker(reviewer))
    await hub.ordinary_turn_completed(reviewer["worker_id"], reviewer["id"])
    return await publish_batch(hub, reviewer["run_id"])


async def publish_batch(hub, run_id):
    """Publish every waiting accepted review as one Integrator batch with synthetic final evidence."""
    from formal_geyser.coordination.batch_integration import BATCH_DELAY_MS

    def admit(c):
        hub._enable_integrator(c)
        # Batches normally wait for five contributions or 13 minutes; admit at once.
        c.execute("UPDATE integrator_control SET last_finished_at=? WHERE id=1", (hub.clock() - BATCH_DELAY_MS,))
        return hub._batch_assignment(c, run_id, None)

    assignment = await hub.db.call(admit, write=True)
    if assignment is None:
        raise AssertionError("No accepted review was ready for integration")
    actor = _worker(assignment)
    runner = Actor(role=Role.RUNNER, worker_id=assignment["worker_id"], attempt_id=assignment["id"],
                   fencing_token=assignment["fencing_token"])

    async def call(name, payload, *, actor=actor, suffix=""):
        return await _command(hub, name, payload, actor=actor)

    await hub.prepare_integration_batch(actor, call)
    batch = await hub.db.call(lambda c: hub._batch(c))
    frozen = hub._repository().finalize(Path(batch["workspace"]), hub.blobs)
    batch = await _command(hub, "integrator.freeze.store", {
        "workspace_id": batch["workspace_id"], "frozen": frozen}, actor=runner)
    policies = await hub.db.call(lambda c: {
        item["candidate_id"]: hub._candidate_binding(c, item["candidate_id"])["policy"]
        for item in batch["items"] if item["state"] == "included"})
    reports = {candidate: {
        "verdict": "accepted", "policy": policy, "qualifications": [],
        "details": {"integration_snapshot": batch["round"]["snapshot_sha"],
                    "scope": "Synthetic final-tree fixture, no mathematical assertion."}}
        for candidate, policy in policies.items()}
    await _command(hub, "integrator.validation", {"round_id": batch["round_id"], "reports": reports}, actor=runner)
    await _command(hub, "integrator.publish", {"round_id": batch["round_id"]}, actor=actor)
    result = await _command(hub, "runtime.quiescent", {
        "worker_id": assignment["worker_id"],
        "snapshot": hub.blobs.snapshot(hub.workspace(assignment["worker_id"]))}, actor=Actor(role=Role.RUNNER))
    await hub.archive_moves_settled()  # The published archives are moved in the background.
    return result
