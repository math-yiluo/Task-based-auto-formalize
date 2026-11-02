"""Published contributions keep their identity, not a place in the Library."""
import json
from pathlib import Path

from formal_geyser.coordination.models import Actor, ProjectConfig, Role
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import ObjectRegistry
from formal_geyser.schemas import EnvironmentRecord
from tests.bootstrap_helpers import synthetic_execution
from tests.conftest import proof_case
from tests.coordination.publication_helpers import approve, new_hub
from tests.coordination.test_archive_publication import archive_case as archive_case
from tests.coordination.test_archive_publication import command
from tests.native_publication_helpers import claim, publish


async def test_later_library_edits_never_invalidate_an_earlier_acceptance(tmp_path):
    """Real Lean schemas/core/Git; explicitly synthetic machine receipts only."""
    fixture, _, _ = await proof_case.__wrapped__(tmp_path / "f")
    hub = synthetic_execution(await new_hub(tmp_path / "p", config=ProjectConfig(domain="lean")))
    try:
        environment = await ObjectRegistry(hub).stage(EnvironmentRecord.model_validate(
            fixture.artifacts["environment-1"]["data"]), Actor(role=Role.OPERATOR))
        await command(hub, "artifact.accept", {"artifact_id": environment["id"],
            "policy": "lean.environment@1", "verdict": "accepted"})
        run = await command(hub, "run.start")

        async def define(title, files, data):
            task = await command(hub, "task.publish", {"contract": {"title": title,
                "objective": data["description"], "domain": "lean", "acceptance_policy": "lean.definition@1",
                "inputs": [environment["id"]], "payload": {"environment_artifact": environment["id"]}}})
            author = await claim(hub, task, run)
            workspace = hub.workspace(author["worker_id"])
            for path, body in files.items():
                (workspace / path).write_bytes(body)
            payload = await LeanPlugin().normalize_submission(hub, task, {"kind": "lean.definition", "data": {
                **data, "provenance": "internal", "provenance_rationale": "Simple software fixture definition."}},
                hub.blobs.snapshot(workspace))
            staged = await command(hub, "artifact.stage", payload, Actor(
                role=Role.WORKER, worker_id=author["worker_id"], attempt_id=author["id"],
                fencing_token=author["fencing_token"]))
            return await publish(hub, staged, run)

        first = await define("Publish one", {
            "Main.lean": b"def original : Nat := 1\n",
            "Main.md": b"The definition original denotes the natural number one.\n"},
            {"name": "One", "description": "The natural number one.", "entry_module": "Main", "declaration": "original"})
        # A later contribution reorganizes the same file; the first acceptance stands.
        await define("Reorganize the Library", {
            "Main.lean": b"import Extra\ndef original : Nat := extra\n",
            "Extra.lean": b"def extra : Nat := 1\n",
            "Extra.md": b"The helper denotes one; original is unchanged in meaning.\n"},
            {"name": "Helper", "description": "A helper for one.", "entry_module": "Extra", "declaration": "extra"})
        retained = await hub.artifact(first["id"])
        assert retained["status"] == "accepted" and "library_membership" not in retained
        assert await hub.db.call(lambda c: hub._available_artifact(c, first["id"]))
        receipt = json.loads((Path(retained["content_file"]["root"]) / "publication.json").read_text())
        assert "source_binding" not in receipt
        assert not (Path(retained["content_file"]["root"]) / "final").exists()
    finally:
        await hub.close()


async def test_withdrawn_artifact_is_unavailable_without_changing_the_library(archive_case):
    from formal_geyser.coordination.sync_state import export_files

    case = archive_case
    await approve(case.hub, case.reviewer)
    artifact_id = case.candidate["artifact_id"]
    before = case.hub._repository().head()
    await command(case.hub, "artifact.withdraw", {"artifact_id": artifact_id, "reason": "Synthetic withdrawal."})
    artifact = await case.hub.artifact(artifact_id)
    assert artifact["status"] == "withdrawn"
    assert not await case.hub.db.call(lambda c: case.hub._available_artifact(c, artifact_id))
    assert case.hub._repository().head() == before
    files, _ = await export_files(case.hub, main_commit=before)
    task = json.loads(files[f"tasks/{case.host['id']}/task.json"])
    assert [item["status"] for item in task["artifacts"]] == ["withdrawn"]
