"""Real native publication boundaries with explicitly synthetic validation receipts."""
from copy import deepcopy
from uuid import uuid4

from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.registry import ObjectRegistry, reference_ids
from formal_geyser.schemas import EnvironmentRecord, StatementRecord
from tests.bootstrap_helpers import synthetic_execution
from tests.coordination.lifecycle_helpers import select_review
from tests.coordination.publication_helpers import publish_batch


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             actor or Actor(role=Role.OPERATOR))


def worker(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


async def claim(hub, task, run):
    identity = await command(hub, "worker.register")
    return await command(hub, "task.claim", {"task_id": task["id"], "revision": task["revision"],
                         "worker_id": identity["id"], "run_id": run["id"]})


def semantic(data):
    pair = data["bundle"]
    finding = {"lean_location": pair["lean_path"] + ":1", "markdown_location": pair["markdown_path"] + ":1",
               "finding": "Exact synthetic software fixture correspondence; no mathematical validation claimed."}
    return {"lean_markdown": [finding],
            "source_checks": [{**citation, **finding} for citation in data.get("sources", [])],
            "provenance_rationale": "Synthetic storage protocol fixture."}


async def publish(hub, staged, run, *, report=None):
    """Synthetic validator receipts exercise storage/reviewer authority, not Lean."""
    artifact = await hub.artifact(staged["artifact_id"])
    policy = artifact["publication_policy"]
    original = report or {"verdict": "accepted", "policy": policy, "details": {"synthetic_storage_fixture": True}}
    await command(hub, "artifact.accept", {"artifact_id": artifact["id"], **original}, Actor(role=Role.VALIDATOR))
    reviewer = await select_review(hub, staged["review_task_id"], run["id"])
    basis = reviewer["task"]["collaboration"]["review"]
    review = {key: basis[key] for key in ("candidate_id", "candidate_hash", "task_revision", "input_refs", "policy")}
    review.update(rationale="Inspect the synthetic frozen pair.",
                  evidence=["Frozen native fixture files."], semantic_checks=semantic(artifact["data"]))
    await command(hub, "review.accept", review, worker(reviewer))
    await hub.ordinary_turn_completed(reviewer["worker_id"], reviewer["id"])
    await publish_batch(hub, run["id"])
    return await hub.artifact(artifact["id"])


async def setup_statement(hub, fixture, *, sources=None):
    synthetic_execution(hub)
    for stored in fixture.blobs.root.rglob("*"):
        if stored.is_file():
            hub.blobs.put(stored.read_bytes())
    environment_data = deepcopy(fixture.artifacts["environment-1"]["data"])
    environment = await ObjectRegistry(hub).stage(EnvironmentRecord.model_validate(environment_data), Actor(role=Role.OPERATOR))
    await command(hub, "artifact.accept", {"artifact_id": environment["id"], "policy": "lean.environment@1", "verdict": "accepted"})
    await command(hub, "repository.initialize", {"snapshot": hub.blobs.put_json({"files": []})})
    data = deepcopy(fixture.artifacts["statement-1"]["data"])
    data["environment_artifact"] = environment["id"]
    if sources:
        data.update(sources=sources, provenance="source", provenance_rationale="")
    task = await command(hub, "task.publish", {"contract": {"title": "Publish synthetic native statement",
        "objective": "Exercise paired native statement publication.", "domain": "lean", "acceptance_policy": "lean.statement@1",
        "inputs": [environment["id"]], "payload": {"environment_artifact": environment["id"]}}})
    run = await command(hub, "run.start")
    author = await claim(hub, task, run)
    for item in hub.blobs.get_json(data["bundle"]["snapshot"])["files"]:
        path = hub.workspace(author["worker_id"]) / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(hub.blobs.get(item["sha256"]))
    staged = await command(hub, "artifact.stage", {"kind": "lean.statement", "data": data,
        "input_refs": reference_ids(StatementRecord.model_validate(data)), "introduction": "Synthetic storage fixture."}, worker(author))
    statement = await publish(hub, staged, run)
    return environment, statement, run
