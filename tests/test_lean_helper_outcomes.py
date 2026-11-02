"""Real Lean target-inspection regressions (opt-in: FORMAL_GEYSER_TEST_LEAN)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

import formal_geyser

HELPERS = Path(formal_geyser.__file__).resolve().parent / "lean_tools"
MODULES = {
    "A": "module\n@[expose] public def f : Nat → Nat\n  | 0 => 1\n  | n+1 => (n+1) * f n\n",
    "S": "module\npublic import A\n@[expose] public def Stmt : Prop := ∀ n, 0 < f n\n"
         "theorem baselineUse (n : Nat) : f (n+1) = (n+1) * f n := by rw [f.eq_def]\n",
    # Imports only A, so it realizes its own copy of `f.eq_def`.
    "Q": "module\npublic import A\n"
         "public theorem candidateUse (n : Nat) : f (n+1) = (n+1) * f n := by rw [f.eq_def]\n",
    "P": "module\npublic import S\npublic import Q\npublic theorem proof : Stmt := by\n  intro n\n"
         "  induction n with\n  | zero => decide\n"
         "  | succ n ih => rw [candidateUse]; exact Nat.mul_pos (Nat.succ_pos n) ih\n",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if not os.environ.get("FORMAL_GEYSER_TEST_LEAN"):
        pytest.skip("Set FORMAL_GEYSER_TEST_LEAN to run the real Lean helper regressions")
    lean = Path(os.environ["FORMAL_GEYSER_TEST_LEAN"]).resolve()
    root = tmp_path_factory.mktemp("lean-helpers")
    environment = {**os.environ, "LEAN_PATH": str(root)}
    for name, source in MODULES.items():
        (root / (name + ".lean")).write_text(source, encoding="utf-8")
        subprocess.run([str(lean), "-o", str(root / (name + ".olean")), str(root / (name + ".lean"))],
                       cwd=root, env=environment, check=True)
    return lean, root, environment


def run(built, helper, *args):
    lean, root, environment = built
    return subprocess.run([str(lean), "--run", str(HELPERS / helper), str(lean.parent.parent), *args],
                          cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8")


def test_inspection_reports_findings_and_unloadable_environments(built):
    finding = run(built, "Inspect.lean", "P", "Missing.target")
    assert finding.returncode == 1 and "finding" in json.loads(finding.stdout)
    _, root, _ = built
    (root / "Q.olean").write_bytes(b"unreadable")
    unloadable = run(built, "Inspect.lean", "P", "proof")
    assert unloadable.returncode == 3 and "Environment unavailable" in unloadable.stderr
