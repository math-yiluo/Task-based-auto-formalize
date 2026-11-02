"""Real native receipt and withdrawal transitions with synthetic checker evidence."""
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    ProjectConfig,
    RepositoryConfig,
    Role,
)
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import ObjectRegistry
from formal_geyser.verification import ProofVerifier
from tests.native_publication_helpers import claim, command, publish, setup_statement, worker


async def test_core_receipts_and_transitive_withdrawal(tmp_path, proof_case, lean_service):
    fixture, _, _ = proof_case
    hub = Hub(tmp_path / "native-core", config=ProjectConfig(domain="lean", repository=RepositoryConfig(enabled=True)))
    try:
        document = tmp_path / "source.txt"
        document.write_text("Software metadata fixture only.", encoding="utf-8")
        source = await ObjectRegistry(hub).import_source(document, Actor(role=Role.OPERATOR))
        citation = {"literature_id": source["id"], "content_sha256": source["source_sha256"], "locator": "line 1"}
        environment, statement, run = await setup_statement(hub, fixture, sources=[citation])
        task = await command(hub, "task.publish", {"contract": {
            "title": "Synthetic native proof protocol", "objective": "Exercise exact native proof receipts.",
            "domain": "lean", "acceptance_policy": "lean.proof@1", "inputs": [environment["id"]],
            "payload": {"environment_artifact": environment["id"]}}})
        author = await claim(hub, task, run)
        workspace = hub.workspace(author["worker_id"])
        (workspace / "Candidate.lean").write_text("-- Synthetic software protocol fixture only.\n", encoding="utf-8")
        (workspace / "Candidate.md").write_text("Synthetic paired explanation.\n", encoding="utf-8")
        payload = await LeanPlugin().normalize_submission(hub, task, {"kind": "lean.proof", "data": {
            "name": "Synthetic proof fixture", "entry_module": "Candidate", "declaration": "Candidate.target",
            "statement_declaration": statement["data"]["declaration"]}}, hub.blobs.snapshot(workspace))
        staged = await command(hub, "artifact.stage", payload, worker(author))
        artifact = await hub.artifact(staged["artifact_id"])
        report = await ProofVerifier().validate(hub, task, artifact)
        assert report.verdict == "accepted", report.details
        # The official check runs in the Lean service on behalf of the candidate's author.
        [check] = [spec for spec in lean_service.specs if spec.label == "Proof candidate " + artifact["id"]]
        assert (check.lane, check.owner) == ("official", author["worker_id"])
        assert check.request.snapshot == artifact["snapshot"]
        assert report.details["statement_declaration"] == statement["data"]["declaration"]
        published = await publish(hub, staged, run, report=report.model_dump(mode="json"))
        assert published["accepted"]
        assert not await hub.list_artifacts(kind="lean.source")
        assert not await hub.list_artifacts(kind="lean.specification")
        assert not await hub.list_artifacts(kind="lean.statement-binding")
        declaration = await command(hub, "task.complete.declare", {"task_id": task["id"], "revision": 1,
            "reason": "Synthetic native receipt path completed.", "evidence": {"objective": {
                "explanation": "Exact paired source and independent fixture validation are published.", "artifact_ids": [published["id"]]}}}, worker(author))
        await command(hub, "task.complete.finalize", {"declaration_id": declaration["id"], "decision": "close", "reason": "Synthetic objective is satisfied."}, worker(author))
        await command(hub, "runtime.quiescent", {"worker_id": author["worker_id"], "snapshot": hub.blobs.snapshot(workspace)}, Actor(role=Role.RUNNER))
        assert (await hub.task(task["id"]))["state"] == "done"
        await command(hub, "artifact.withdraw", {"artifact_id": environment["id"], "reason": "Retract synthetic environment fixture."})
        for identity in (environment["id"], statement["id"], published["id"]):
            assert (await hub.artifact(identity))["status"] == "withdrawn"
        assert (await hub.accessible_literature(source["id"]))["source_sha256"] == source["source_sha256"]
    finally:
        await hub.close()
