"""Generic note final checks preserve metadata and resolve exact environments."""
import sys
from pathlib import Path

import pytest

from formal_geyser.coordination.models import (
    Actor,
    Role,
    ValidationReport,
)
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import ObjectRegistry, RegistryError, file_sha256
from formal_geyser.schemas import EnvironmentRecord
from formal_geyser.snapshot_policy import submission_environment, submission_limits
from tests.native_publication_helpers import command
from tests.test_native_note_publication import note_task, snapshot
from tests.test_native_note_publication import public_tree as public_tree


async def environment(hub, name="Synthetic note environment"):
    executable = Path(sys.executable).resolve()
    value = await ObjectRegistry(hub).stage(EnvironmentRecord(name=name,
        lean_executable=str(executable), lean_executable_sha256=file_sha256(executable),
        lean_version="synthetic", local_execution="trusted-code-only"), Actor(role=Role.OPERATOR))
    await command(hub, "artifact.accept", {"artifact_id": value["id"], "policy": "lean.environment@1",
        "verdict": "accepted", "details": {"synthetic_fixture": True}})
    return value


@pytest.mark.parametrize("selection", ["input", "payload", "record"])
async def test_final_note_uses_same_environment_as_bounds(public_tree, monkeypatch, selection):
    hub, base, original = public_tree
    env = await environment(hub)
    task = note_task()
    task["contract"]["inputs"] = [env["id"]]
    data = {"summary": "Reviewed wiki metadata"}
    if selection == "payload":
        task["contract"]["payload"]["environment_artifact"] = env["id"]
    if selection == "record":
        data["environment_artifact"] = env["id"]
    sha = snapshot(hub, {**original, "wiki/Start.md": b"# Reviewed shared note\n"})
    artifact = {"id": "synthetic-note", "kind": "note.wiki", "data": data, "snapshot": sha,
                "input_refs": [env["id"]], "integration_base_commit": base}
    seen = []
    async def build(self, hub, task, artifact):
        seen.append(artifact["data"]["environment_artifact"])
        return ValidationReport(verdict="accepted", policy="lean.build@1", details={"synthetic_fixture": True})
    monkeypatch.setattr("formal_geyser.plugin.BuildVerifier.validate", build)
    assert await submission_environment(hub, task, artifact) == env["id"]
    assert (await submission_limits(hub, task, artifact))["max_files"] == 2048
    plugin = LeanPlugin()
    assert (await plugin.normalize_submission(hub, task, artifact, sha))["data"] == data
    assert (await plugin.validate_candidate(hub, task, artifact)).verdict == "accepted"
    final = await plugin.validate_final_tree(hub, [{"task": task, "artifact": artifact}], sha)
    assert final.verdict == "accepted" and seen[-1] == env["id"]


async def test_note_environment_ambiguity_and_conflicting_pin_fail_before_build(public_tree):
    hub, base, original = public_tree
    one, two = await environment(hub, "One"), await environment(hub, "Two")
    task = note_task()
    task["contract"]["inputs"] = [one["id"], two["id"]]
    sha = snapshot(hub, original)
    artifact = {"id": "note", "kind": "supporting.note", "data": {}, "snapshot": sha,
                "integration_base_commit": base}
    plugin = LeanPlugin()
    with pytest.raises(RegistryError, match="unambiguous"):
        await plugin.normalize_submission(hub, task, artifact, sha)
    report = await plugin.validate_final_tree(hub, [{"task": task, "artifact": artifact}], sha)
    assert report.verdict == "rejected" and "unambiguous" in report.details["reason"]
    task["contract"]["payload"]["environment_artifact"] = one["id"]
    assert await submission_environment(hub, task, artifact) == one["id"]
    artifact["data"]["environment_artifact"] = "missing-other-environment"
    with pytest.raises(RegistryError, match="authoritative value"):
        await submission_limits(hub, task, artifact)
