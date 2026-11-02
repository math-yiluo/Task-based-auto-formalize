"""Compilation-evidence policy tests; compiler output is mocked and no mathematical work is run."""
import errno
import json

import pytest

from formal_geyser.build_verification import BUILD_POLICY, BuildVerifier
from formal_geyser.coordination.models import TaskContract
from formal_geyser.lean_service import client
from formal_geyser.plugin import LeanPlugin
from formal_geyser.process import ProcessResult
from formal_geyser.verification import ProofVerifier
from tests.conftest import fake_build_outputs, fake_kernel_replay


class BuildRunner:
    def __init__(self, *, error=False, warning=False, timed_out=False, rewrite_source=False):
        self.calls = []
        self.error = error
        self.warning = warning
        self.timed_out = timed_out
        self.rewrite_source = rewrite_source
    async def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "--version" in argv:
            return ProcessResult(tuple(argv), 0, "synthetic-version\n", "", 0.01)
        if (replayed := fake_kernel_replay(argv)) is not None:
            return replayed
        fake_build_outputs(kwargs["cwd"])
        if self.rewrite_source:
            # Lean may run a source's own code while it compiles it.
            (kwargs["cwd"] / "source" / "Candidate.lean").write_text("-- Rewritten by its own build.\n")
        stdout = ""
        if self.error or self.warning:
            stdout = json.dumps({"severity": "error" if self.error else "warning", "data": "Synthetic diagnostic."})
        return ProcessResult(tuple(argv), 1 if self.error else 0, stdout, "", 0.01,
                             timed_out=self.timed_out)


async def test_build_acceptance_has_narrow_explicit_scope(build_case, lean_service):
    hub, task, artifact = build_case
    LeanPlugin().validate_contract(TaskContract.model_validate(task["contract"]))
    lean_service.runner = BuildRunner()
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "accepted"
    assert report.policy == BUILD_POLICY
    assert report.details["acceptance_scope"] == "source-compilation-only"
    assert report.details["build_succeeded"]
    assert report.details["proof_accepted"] is False
    assert report.details["axiom_policy_checked"] is False
    assert report.details["mathematical_fidelity_reviewed"] is False
    assert not report.qualifications
    assert not hub.commands
    # One official Lean service check builds the frozen snapshot and replays it through the kernel.
    [spec] = lean_service.specs
    assert (spec.lane, spec.owner, spec.label) == ("official", "validation", "Build candidate build-1")
    assert (spec.request.snapshot, spec.request.directory) == (artifact["snapshot"], None)
    assert spec.request.modules == report.details["checked_modules"] and spec.request.kernel_replay
    assert not spec.request.inspections
    assert report.details["doctor"]["ok"]


async def test_build_warnings_remain_visible_without_becoming_proof_evidence(build_case, lean_service):
    hub, task, artifact = build_case
    lean_service.runner = BuildRunner(warning=True)
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "accepted"
    assert report.details["compilation"]["diagnostics"]["warnings"] == 1
    assert report.details["proof_accepted"] is False


@pytest.mark.parametrize("field,value", [("task_revision", 1), ("environment_artifact", "other"),
                                          ("modules", ["Different"]), ("snapshot", "0" * 64)])
async def test_build_cannot_rebind_task_environment_modules_or_snapshot(build_case, lean_service, field, value):
    hub, task, artifact = build_case
    artifact["data"][field] = value
    lean_service.runner = runner = BuildRunner()
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "rejected"
    assert not runner.calls and not lean_service.specs


async def test_build_requires_environment_approval(build_case, lean_service):
    hub, task, artifact = build_case
    hub.artifacts["environment-1"]["evidence"] = []
    lean_service.runner = BuildRunner()
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "needs_operator"


async def test_build_honors_disabled_local_execution(build_case, lean_service):
    hub, task, artifact = build_case
    hub.artifacts["environment-1"]["data"]["local_execution"] = "disabled"
    lean_service.runner = runner = BuildRunner()
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "unavailable"
    assert not runner.calls and not lean_service.specs


async def test_build_requires_dependency_edge(build_case, lean_service):
    hub, task, artifact = build_case
    artifact["input_refs"] = []
    lean_service.runner = BuildRunner()
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "rejected"
    assert "dependency edge" in report.details["reason"]


@pytest.mark.parametrize("failure", ["error", "timed_out"])
async def test_build_does_not_accept_failed_compilation(build_case, lean_service, failure):
    hub, task, artifact = build_case
    lean_service.runner = BuildRunner(**{failure: True})
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == ("unavailable" if failure == "timed_out" else "rejected")
    assert report.details["proof_accepted"] is False


async def test_build_that_rewrites_its_frozen_source_is_rejected(build_case, lean_service):
    # The Lean service compares its build slot's copy of the sources with the snapshot.
    hub, task, artifact = build_case
    lean_service.runner = BuildRunner(rewrite_source=True)
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "rejected"
    assert report.details["compilation"]["reason"] == "Compilation changed a frozen source file"
    assert "build_succeeded" not in report.details


@pytest.mark.parametrize("payload", [{"environment_artifact": "environment-1"},
                                     {"environment_artifact": "environment-1", "modules": ["Candidate", "Candidate"]},
                                     {"modules": ["Candidate"]}])
def test_build_contract_requires_frozen_environment_and_explicit_unique_modules(build_case, payload):
    _, task, _ = build_case
    contract = TaskContract.model_validate(task["contract"]).model_copy(update={"payload": payload})
    with pytest.raises(ValueError):
        LeanPlugin().validate_contract(contract)


async def test_proof_policy_uses_lake(proof_case):
    hub, task, artifact = proof_case
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "accepted"
    assert report.policy == "lean.proof@1"


async def test_build_rejects_precompiled_candidate_files(build_case, lean_service):
    hub, task, artifact = build_case
    entries = hub.blobs.get_json(artifact["snapshot"])["files"]
    entries.append({"path": "Candidate.olean", "sha256": hub.blobs.put(b"not source")})
    artifact["snapshot"] = hub.blobs.put_json({"files": entries})
    lean_service.runner = BuildRunner()
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "rejected"
    assert "precompiled" in report.details["reason"]
    assert not lean_service.specs


async def test_final_tree_is_one_scan_and_one_build_recorded_for_every_item(build_case, lean_service):
    from formal_geyser.batch_validation import validate_batch

    hub, task, artifact = build_case
    lean_service.runner = runner = BuildRunner()
    items = [{"task": task, "artifact": artifact}, {"task": task, "artifact": {**artifact, "id": "build-2"}}]
    reports = await validate_batch(LeanPlugin(), hub, items, artifact["snapshot"])
    assert set(reports) == {"build-1", "build-2"}
    for report in reports.values():
        assert report["verdict"] == "accepted" and report["policy"] == BUILD_POLICY
        assert set(report["details"]["checked_modules"]) == {"Baseline", "Candidate"}
        assert report["details"]["integration_snapshot"] == artifact["snapshot"]
    assert len([argv for argv, _ in runner.calls if "build" in argv]) == 1
    # The Integrator's build is one check in its own lane of the Lean service.
    [spec] = lean_service.specs
    assert (spec.lane, spec.owner, spec.label) == ("integrator", "integrator", "Integrated Library build-1")
    assert spec.request.snapshot == artifact["snapshot"]


async def test_final_tree_hole_rejects_before_building(build_case, lean_service):
    hub, task, artifact = build_case
    lean_service.runner = runner = BuildRunner()
    files = [*hub.blobs.get_json(artifact["snapshot"])["files"],
             {"path": "Later.lean", "sha256": hub.blobs.put(b"theorem later : 1 = 1 := by sorry\n")},
             {"path": "Later.md", "sha256": hub.blobs.put(b"A later theorem.\n")}]
    snapshot = hub.blobs.put_json({"files": sorted(files, key=lambda item: item["path"])})
    report = await LeanPlugin().validate_final_tree(hub, [{"task": task, "artifact": artifact}], snapshot)
    assert report.verdict == "rejected" and "holes" in report.details["reason"]
    assert not runner.calls and not lean_service.specs


async def test_final_tree_requires_same_stem_markdown_for_every_lean_file(build_case, lean_service):
    hub, task, artifact = build_case
    lean_service.runner = runner = BuildRunner()
    files = [*hub.blobs.get_json(artifact["snapshot"])["files"],
             {"path": "Split.lean", "sha256": hub.blobs.put(b"def split := 1\n")}]
    snapshot = hub.blobs.put_json({"files": sorted(files, key=lambda item: item["path"])})
    report = await LeanPlugin().validate_final_tree(hub, [{"task": task, "artifact": artifact}], snapshot)
    assert report.verdict == "rejected" and "same-stem Markdown: Split.lean" in report.details["reason"]
    assert not runner.calls and not lean_service.specs


@pytest.mark.parametrize("stage", ["candidate", "final_tree"])
async def test_a_failing_disk_makes_a_check_unavailable_not_a_rejection(build_case, lean_service, monkeypatch, stage):
    # Unavailable runs again later; rejected would send correct work back to its author.
    hub, task, artifact = build_case
    lean_service.runner = runner = BuildRunner()

    def failing(sha):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(hub.blobs, "get", failing)
    monkeypatch.setattr(hub.blobs, "get_json", failing)
    if stage == "candidate":
        report = await LeanPlugin().validate_candidate(hub, task, artifact)
    else:
        report = await LeanPlugin().validate_final_tree(hub, [{"task": task, "artifact": artifact}], artifact["snapshot"])
    assert report.verdict == "unavailable" and "Input/output error" in report.details["reason"]
    assert not runner.calls and not lean_service.specs


class StillRunning:
    """A Lean service whose checks are still queued when their callers stop waiting."""

    def __init__(self):
        self.specs = []

    async def submit(self, spec):
        self.specs.append(spec)
        return {"id": "job-1", "state": "queued", "position": 2, "lane": spec["lane"], "label": spec["label"]}


@pytest.mark.parametrize("candidate", ["build", "proof"])
async def test_a_check_still_running_in_the_lean_service_is_not_a_verdict(build_case, proof_case, candidate):
    # Past a worker's bounded wait the official check goes on in the Lean service: validation
    # gives no verdict, so the stage reply says it is queued and asking again attaches to it.
    hub, task, artifact = build_case if candidate == "build" else proof_case
    service = StillRunning()
    previous = client.install(lambda root: service)
    try:
        with client.waiting(0), pytest.raises(client.LeanJobPending) as pending:
            await LeanPlugin().validate_candidate(hub, task, artifact)
    finally:
        client.install(previous)
    assert pending.value.job["state"] == "queued"
    [spec] = service.specs
    assert (spec["lane"], spec["request"]["snapshot"]) == ("official", artifact["snapshot"])
