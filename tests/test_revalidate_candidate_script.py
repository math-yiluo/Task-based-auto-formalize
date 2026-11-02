"""Maintenance appends actual fresh evidence without publishing or replaying cached output."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from formal_geyser import maintenance
from formal_geyser.coordination.models import Role, ValidationReport
from tests.coordination.publication_helpers import new_hub
from tests.coordination.test_collaborative_core import command


@pytest.mark.parametrize("kind", ["definition", "statement", "proof"])
@pytest.mark.parametrize("verdict", ["accepted", "rejected", "needs_operator", "unavailable"])
async def test_records_exact_fresh_report_and_preserves_candidate(monkeypatch, verdict, kind):
    original = {"id": "candidate", "kind": f"lean.{kind}", "status": "staged",
                "task_id": "host", "task_revision": 3, "publication_policy": f"lean.{kind}@1",
                "data": {"qualifications": ["Author scope note."]}, "snapshot": "frozen"}
    candidate = deepcopy(original)
    calls = []

    async def artifact(_):
        return candidate

    async def task(task_id, *, revision):
        assert (task_id, revision) == ("host", 3)
        return {"id": task_id, "revision": revision, "contract": {"acceptance_policy": "lean.peer@1"}}

    async def execute(action, actor):
        calls.append((action, actor))

    report = ValidationReport(verdict=verdict, policy=f"lean.{kind}@1",
        details={"checked_exact_snapshot": "frozen"},
        qualifications=[] if verdict == "accepted" else ["Actual verifier finding."])

    class Verifier:
        async def validate_candidate(self, hub, pinned, received):
            assert pinned["revision"] == 3
            assert pinned["contract"]["acceptance_policy"] == f"lean.{kind}@1"
            assert received == original
            return report

    monkeypatch.setattr(maintenance, "LeanPlugin", Verifier)
    hub = SimpleNamespace(artifact=artifact, task=task, execute=execute)
    dry = await maintenance.revalidate(hub, "candidate", record=False)
    assert not calls and not dry["recorded"]
    result = await maintenance.revalidate(hub, "candidate", record=True)
    assert result["recorded"] and result["report"] == report.model_dump(mode="json")
    assert len(calls) == 1
    action, actor = calls[0]
    assert action.name == "artifact.accept" and actor.role == Role.VALIDATOR
    assert action.payload == {"artifact_id": "candidate", **report.model_dump(mode="json")}
    assert candidate == original


async def test_maintenance_requires_paused_quiet_execution(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        run = await command(hub, "run.start")
        with pytest.raises(ValueError, match="Pause"):
            maintenance.require_quiet_project(tmp_path)
        await command(hub, "run.pause", {"run_id": run["id"]})
        maintenance.require_quiet_project(tmp_path)
        marker = tmp_path / ".task-based/owned-processes/check.json"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text('{"process_quiet": false}', encoding="utf-8")
        with pytest.raises(ValueError, match="shutdown confirmation"):
            maintenance.require_quiet_project(tmp_path)
    finally:
        await hub.close()


async def test_receipt_left_by_a_dead_service_is_confirmed_by_its_cgroup(tmp_path, monkeypatch):
    hub = await new_hub(tmp_path)
    await hub.close()
    cgroup = "/owner/tree-" + "0" * 32
    marker = tmp_path / ".task-based/owned-processes/tree-dead.json"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text('{"format": 2, "cgroup": "%s", "process_quiet": false}' % cgroup, encoding="utf-8")
    trees = {cgroup: True}
    monkeypatch.setattr(maintenance, "tree_quiet", lambda receipt, **options: trees[receipt["cgroup"]])
    maintenance.require_quiet_project(tmp_path)  # The service died before marking it quiet.
    trees[cgroup] = False
    with pytest.raises(ValueError, match="shutdown confirmation"):
        maintenance.require_quiet_project(tmp_path)
