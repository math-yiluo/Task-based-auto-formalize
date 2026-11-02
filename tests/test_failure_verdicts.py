"""Only a reported finding rejects; a failed environment or crashed tool is unavailable.

Under memory pressure Lean could not read intact library files, and those runs were
recorded as rejections of correct candidates.
"""
import json

import pytest

from formal_geyser.process import ProcessResult
from formal_geyser.schemas import EnvironmentRecord
from formal_geyser.tooling import LeanTooling, failed_check_verdict, parse_diagnostics

UNREADABLE = {"severity": "error", "pos": {"line": 1, "column": 0},
              "data": "failed to read file '/lib/Mathlib/RingTheory/FiniteType.olean.private'"}
ELABORATION = {"severity": "error", "pos": {"line": 4, "column": 2}, "data": "unknown identifier 'lemma'"}
FINDING = json.dumps({"protocol": "task-based-lean-inspect@1", "finding": "unknown constant 'Missing.target'"})
SEGMENTATION_FAULT = -11  # The returncode of a process that SIGSEGV ended.


def compiled(returncode, *messages):
    result = ProcessResult(("lean",), returncode, "\n".join(map(json.dumps, messages)), "", 0.1)
    return {"ok": False, "modules": [{"module": "M", "process": result.to_dict(),
                                      "diagnostics": parse_diagnostics(result)}]}


def helper(returncode, stdout="", stderr=""):
    return {"ok": False, "modules": [{"module": "M", "process": ProcessResult(
        ("lean",), returncode, stdout, stderr, 0.1).to_dict()}]}


@pytest.mark.parametrize("report,verdict", [
    (compiled(1, ELABORATION), "rejected"),
    (compiled(1, UNREADABLE, ELABORATION), "rejected"),
    (compiled(1, UNREADABLE), "unavailable"),
    (compiled(1), "unavailable"),  # For example "INTERNAL PANIC: out of memory".
    (compiled(SEGMENTATION_FAULT), "unavailable"),
    (compiled(0, ELABORATION), "rejected"),
    (helper(1, FINDING), "rejected"),
    (helper(1, "", "INTERNAL PANIC: out of memory"), "unavailable"),
    (helper(3, "", "Environment unavailable: failed to read file 'Basic.olean'"), "unavailable"),
    ({"ok": False, "modules": helper(0)["modules"], "reason": "Disallowed or locally declared axioms"}, "rejected"),
    ({"ok": False, "reason": "Compilation changed its source file"}, "rejected"),
])
def test_only_a_reported_finding_rejects(report, verdict):
    assert failed_check_verdict(report) == verdict


class HelperRunner:
    def __init__(self, returncode, stdout="", stderr=""):
        self.result = (returncode, stdout, stderr)

    async def run(self, argv, **kwargs):
        if "--version" in argv:
            return ProcessResult(tuple(argv), 0, "synthetic-version\n", "", 0.01)
        return ProcessResult(tuple(argv), *self.result, 0.01)


@pytest.mark.parametrize("returncode,stdout,stderr,verdict", [
    (3, "", "Environment unavailable: failed to read file 'Doc.olean.private'", "unavailable"),
    (1, "", "INTERNAL PANIC: out of memory", "unavailable"),
    (1, FINDING, "unknown constant 'Missing.target'", "rejected"),
    (0, "not the inspector protocol", "", "unavailable"),
    (0, json.dumps({"protocol": "task-based-lean-inspect@1"}), "", "unavailable"),
])
async def test_inspection_failures(proof_case, tmp_path, returncode, stdout, stderr, verdict):
    hub, _, _ = proof_case
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"]).model_copy(
        update={"local_execution": "trusted-code-only"})
    tooling = LeanTooling(environment, hub.blobs, HelperRunner(returncode, stdout, stderr))
    inspection = await tooling.inspect(tmp_path, "Candidate", "Candidate.target")
    assert not inspection["ok"]
    assert failed_check_verdict(inspection) == verdict


async def test_compilation_of_an_unreadable_import_is_unavailable(proof_case, tmp_path):
    hub, _, _ = proof_case
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"]).model_copy(
        update={"local_execution": "trusted-code-only"})
    (tmp_path / "Candidate.lean").write_text("import Mathlib\n", encoding="utf-8")
    report = await LeanTooling(environment, hub.blobs, HelperRunner(1, json.dumps(UNREADABLE))).compile(
        tmp_path, ["Candidate"])
    assert not report["ok"] and failed_check_verdict(report) == "unavailable"


@pytest.mark.parametrize("message", [
    "(deterministic) timeout at `whnf`, maximum number of heartbeats (200000) has been reached",
    "maximum recursion depth has been reached",
])
def test_compile_resource_limits_are_source_findings_but_inspector_limits_are_not(message):
    diagnostic = {**ELABORATION, "data": message}
    assert failed_check_verdict(compiled(1, diagnostic)) == "rejected"
    assert failed_check_verdict(helper(1, json.dumps({"finding": message}))) == "unavailable"
    # A separate real error remains a finding even if another operation exhausted resources.
    assert failed_check_verdict(compiled(1, diagnostic, ELABORATION)) == "rejected"
