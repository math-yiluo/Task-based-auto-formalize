"""An isolated pass diagnoses a parallel failure; it never erases that failure."""
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_parallel.py"


@pytest.fixture
def suite(tmp_path, monkeypatch):
    main = runpy.run_path(str(SCRIPT))["main"]
    state = main.__globals__
    monkeypatch.setitem(state, "ROOT", tmp_path)
    monkeypatch.setitem(state, "DURATIONS", tmp_path / "durations.json")
    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "__init__.py").touch()
    path = directory / "test_probe.py"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "-j", "1", "--rerun-failures"])
    return main, path


@pytest.mark.parametrize("second,outcome", [("pass", "PASS"), ("assert False", "FAIL"),
                                            ("pytest.skip('unavailable')", "NOT_EXECUTED")])
def test_isolated_rerun_reports_separately_and_keeps_failure(suite, capsys, second, outcome):
    main, path = suite
    path.write_text(f"""from pathlib import Path
import pytest

class TestGroup:
    @pytest.mark.parametrize('value', ['id.with.dots/and/slashes'])
    def test_once(self, value):
        marker = Path(__file__).with_suffix('.marker')
        attempted = marker.exists()
        marker.touch()
        assert attempted, 'first attempt fails'
        {second}
""", encoding="utf-8")
    assert main() == 1
    output = capsys.readouterr().out
    assert "1 failed" in output
    assert f"ISOLATED {outcome} tests/test_probe.py::TestGroup::test_once[id.with.dots/and/slashes]" in output
    durations = json.loads((path.parents[1] / "durations.json").read_text())
    assert list(durations) == ["tests/test_probe.py"]


def test_nonzero_partition_exit_without_a_test_failure_is_still_failure(suite, monkeypatch, capsys):
    main, path = suite
    path.write_text("def test_ok(): pass", encoding="utf-8")
    report = path.parent / "report.xml"
    report.write_text('<testsuites><testsuite><testcase classname="tests.test_probe" '
                      'name="test_ok" time="0.1" /></testsuite></testsuites>', encoding="utf-8")
    log_path = path.parent / "partition.log"
    log = log_path.open("wb")
    monkeypatch.setitem(main.__globals__, "start_pytest", lambda *args: (
        report, log, SimpleNamespace(wait=lambda: None, returncode=3)))
    assert main() == 1
    output = capsys.readouterr().out
    assert "partition 0 exited 3" in output and "ISOLATED" not in output


def test_isolation_is_opt_in(suite, monkeypatch, capsys):
    main, path = suite
    path.write_text("def test_bad(): assert False", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "-j", "1"])
    assert main() == 1
    assert "ISOLATED" not in capsys.readouterr().out


def test_empty_filtered_partition_does_not_fail_selected_tests(suite, monkeypatch, capsys):
    main, path = suite
    path.write_text("def test_selected(): pass", encoding="utf-8")
    path.with_name("test_other.py").write_text("def test_other(): pass", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "-j", "2", "-k", "selected"])
    assert main() == 0
    assert "1 tests, 0 failed, 0 errors" in capsys.readouterr().out
