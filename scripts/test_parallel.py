"""Run the test suite in parallel pytest processes without extra dependencies.

Usage: uv run python scripts/test_parallel.py [-j N] [--rerun-failures] [pytest args...]

Test files are split into N balanced partitions (by recorded duration when
available, otherwise by file size). Each partition gets its own temporary
directory and JUnit report; a combined summary is printed at the end.
Optional isolated reruns diagnose failures; they never turn a failed run green.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DURATIONS = ROOT / ".pytest_cache" / "file-durations.json"


def test_files() -> list[Path]:
    return sorted(path for path in (ROOT / "tests").rglob("test_*.py"))


def partitions(files: list[Path], count: int) -> list[list[Path]]:
    try:
        known = json.loads(DURATIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    def cost(path: Path) -> float:
        relative = path.relative_to(ROOT).as_posix()
        return float(known.get(relative, path.stat().st_size / 2000))
    groups: list[tuple[float, list[Path]]] = [(0.0, []) for _ in range(count)]
    for path in sorted(files, key=cost, reverse=True):
        index = min(range(count), key=lambda i: groups[i][0])
        total, members = groups[index]
        groups[index] = (total + cost(path), members + [path])
    return [members for _, members in groups if members]


def case_node(case: ElementTree.Element, files: list[Path]) -> tuple[Path, str] | None:
    """Map JUnit class/function names to exact pytest node IDs, retaining parameters."""
    classname, name = case.get("classname", ""), case.get("name", "")
    for path in sorted(files, key=lambda p: len(p.parts), reverse=True):
        relative = path.relative_to(ROOT)
        prefix = ".".join(relative.with_suffix("").parts)
        if classname == prefix or classname.startswith(prefix + "."):
            classes = classname.removeprefix(prefix).strip(".")
            parts = [relative.as_posix(), *classes.split(".")] if classes else [relative.as_posix()]
            return path, "::".join([*parts, name])
        if not classname and name.split("::", 1)[0].replace("\\", "/") == relative.as_posix():
            return path, name  # Collection failure: rerun this file, once.
    return None


def start_pytest(work: Path, label: str, targets: list[str], extra: list[str]):
    report = work / f"{label}.xml"
    log = (work / f"{label}.log").open("wb")
    base = work / label.replace("part-", "b").replace("isolated-", "r")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "-o", "tmp_path_retention_policy=failed",
               f"--basetemp={base}", f"--junitxml={report}", *extra, *targets]
    try:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    except BaseException:
        log.close()
        raise
    return report, log, process


def rerun_failures(nodes: list[str], work: Path, extra: list[str]) -> None:
    print("Isolated diagnostics (original failures and exit status are retained):")
    for index, node in enumerate(dict.fromkeys(nodes)):
        report, log, process = start_pytest(work, f"isolated-{index}", [node], extra)
        process.wait()
        log.close()
        try:
            cases = list(ElementTree.parse(report).getroot().iter("testcase"))
            bad = any(case.find("failure") is not None or case.find("error") is not None for case in cases)
            skipped = any(case.find("skipped") is not None for case in cases)
            outcome = ("FAIL" if process.returncode not in (0, 5) or bad else "NOT_EXECUTED" if not cases or skipped
                       else "PASS")
        except (OSError, ElementTree.ParseError):
            outcome = "NO_REPORT"
        print(f"ISOLATED {outcome} {node}; see {log.name}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--rerun-failures", action="store_true",
                        help="Rerun each failed test once, serially; preserve the original failure status")
    options, extra = parser.parse_known_args()
    work = Path(tempfile.mkdtemp(prefix="formal-geyser-tests-"))
    groups = partitions(test_files(), max(1, options.jobs))
    started = time.monotonic()
    processes = []
    for index, group in enumerate(groups):
        processes.append((index, group, *start_pytest(work, f"part-{index}",
                                                    [str(path) for path in group], extra)))
    durations: dict[str, float] = {}
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failed: list[str] = []
    nodes: list[str] = []
    for index, group, report, log, process in processes:
        process.wait()
        log.close()
        # -k/-m can legitimately select nothing in one partition.
        if process.returncode not in (0, 5):
            failed.append(f"partition {index} exited {process.returncode}; see {log.name}")
        try:
            cases = list(ElementTree.parse(report).getroot().iter("testcase"))
        except (OSError, ElementTree.ParseError):
            failed.append(f"partition {index} produced no readable report; see {log.name}")
            continue
        for case in cases:
            totals["tests"] += 1
            identity = case_node(case, group)
            if identity:
                name = identity[0].relative_to(ROOT).as_posix()
                durations[name] = durations.get(name, 0.0) + float(case.get("time", 0))
            if case.find("skipped") is not None:
                totals["skipped"] += 1
            for kind in ("failure", "error"):
                if case.find(kind) is not None:
                    totals["failures" if kind == "failure" else "errors"] += 1
                    failed.append(f"{case.get('classname')}::{case.get('name')}")
                    if identity:
                        nodes.append(identity[1])
                    else:
                        failed.append("No isolated target could be identified for this report entry")
    if not totals["tests"]:
        failed.append("No tests were collected across any partition")
    if durations:
        DURATIONS.parent.mkdir(exist_ok=True)
        DURATIONS.write_text(json.dumps(durations, indent=1, sort_keys=True), encoding="utf-8")
    elapsed = time.monotonic() - started
    print(f"{totals['tests']} tests, {totals['failures']} failed, {totals['errors']} errors, "
          f"{totals['skipped']} skipped in {elapsed:.0f}s across {len(groups)} processes; logs in {work}")
    for name in failed:
        print("FAILED", name)
    if options.rerun_failures and nodes:
        rerun_failures(nodes, work, extra)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
