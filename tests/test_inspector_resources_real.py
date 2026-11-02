"""Real inspection of ordinary data, proposition targets, and resource failures."""
import json
import os
import subprocess
from pathlib import Path

import pytest

import formal_geyser


def test_inspector_handles_data_and_reports_exhaustion_without_rejecting(tmp_path):
    if not os.environ.get("FORMAL_GEYSER_TEST_LEAN"):
        pytest.skip("Set FORMAL_GEYSER_TEST_LEAN for real inspector checks")
    lean = Path(os.environ["FORMAL_GEYSER_TEST_LEAN"])
    helper = Path(formal_geyser.__file__).parent / "lean_tools/Inspect.lean"
    source = tmp_path / "Resources.lean"
    source.write_text("module\n@[expose] public def ordinary (n : Nat) : Nat := n\n"
                      "@[expose] public def statement : Prop := True\n"
                      "@[expose] public def countdown : Nat → Prop\n"
                      "  | 0 => True\n  | n+1 => countdown n\n"
                      "@[expose] public def deep : Prop := countdown 5000\n", encoding="utf-8")
    env = {**os.environ, "LEAN_PATH": str(tmp_path)}
    subprocess.run([str(lean), "-o", str(tmp_path / "Resources.olean"), str(source)],
                   cwd=tmp_path, env=env, check=True, capture_output=True)
    def inspect(name, *output):
        return subprocess.run([str(lean), "--run", str(helper), str(lean.parent.parent),
                               "Resources", name, *output], cwd=tmp_path, env=env,
                              capture_output=True, text=True, encoding="utf-8")
    ordinary = inspect("ordinary")
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert json.loads(ordinary.stdout)["statement_returns_prop"] is False
    statement = inspect("statement")
    assert statement.returncode == 0, statement.stdout + statement.stderr
    assert json.loads(statement.stdout)["statement_returns_prop"] is True
    # With --output the exact expressions go to their own files, byte for byte as printed.
    printed = json.loads(statement.stdout)
    for name, expected_target in (("statement", printed["statement_target_repr"]), ("ordinary", None)):
        result = tmp_path / (name + ".json")
        written = inspect(name, "--output", str(result))
        assert written.returncode == 0, written.stdout + written.stderr
        fields = json.loads(result.read_text(encoding="utf-8"))
        assert fields["protocol"] == "task-based-lean-inspect@3" and "type_repr" not in fields
        assert fields["type_encoding"] == "repr"
        assert fields["statement_target_encoding"] == ("repr" if expected_target is not None else None)
        assert (tmp_path / (name + ".json.type_repr")).is_file()
        target = tmp_path / (name + ".json.statement_target_repr")
        assert (target.read_text(encoding="utf-8") if target.exists() else None) == expected_target
    assert (tmp_path / "statement.json.type_repr").read_text(encoding="utf-8") == printed["type_repr"]
    deep = inspect("deep")
    assert deep.returncode in {0, 3}, deep.stdout + deep.stderr
    if deep.returncode == 3:
        assert "unavailable" in deep.stderr and "finding" not in deep.stdout
    else:
        assert json.loads(deep.stdout)["statement_returns_prop"] is True


def test_an_expression_too_large_written_out_is_written_with_its_sharing(tmp_path):
    # Expr.repr spells out each shared subterm at every use: in the pilot statements written out
    # ran to 7 GB and took the machine's memory. This type is And applied to itself 40 times over.
    if not os.environ.get("FORMAL_GEYSER_TEST_LEAN"):
        pytest.skip("Set FORMAL_GEYSER_TEST_LEAN for real inspector checks")
    lean = Path(os.environ["FORMAL_GEYSER_TEST_LEAN"])
    helper = Path(formal_geyser.__file__).parent / "lean_tools/Inspect.lean"
    source = tmp_path / "Tower.lean"
    source.write_text("import Lean\nopen Lean Elab Command\n\n"
                      "elab \"add_tower\" : command => liftCoreM do\n"
                      "  let mut e := mkConst ``True\n"
                      "  for _ in [0:40] do\n"
                      "    e := mkApp2 (mkConst ``And) e e\n"
                      "  addDecl (.axiomDecl { name := `tower, levelParams := [], type := e, isUnsafe := false })\n\n"
                      "add_tower\n", encoding="utf-8")
    env = {**os.environ, "LEAN_PATH": str(tmp_path)}
    subprocess.run([str(lean), "-o", str(tmp_path / "Tower.olean"), str(source)],
                   cwd=tmp_path, env=env, check=True, capture_output=True)
    result = tmp_path / "tower.json"
    inspected = subprocess.run([str(lean), "--run", str(helper), str(lean.parent.parent), "Tower", "tower",
                                "--output", str(result)], cwd=tmp_path, env=env, capture_output=True,
                               text=True, encoding="utf-8", timeout=300)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    fields = json.loads(result.read_text(encoding="utf-8"))
    assert fields["type_encoding"] == "shared" and fields["axioms"] == ["tower"]
    lines = (tmp_path / "tower.json.type_repr").read_text(encoding="utf-8").splitlines()
    # And, True, and at each of the forty levels `And e` and `And e e`: each distinct subterm once.
    assert lines[:4] == ["0 const `And []", "1 const `True []", "2 app 0 1", "3 app 2 1"]
    assert len(lines) == 82 and lines[-1] == "81 app 80 79"
