"""The cgroups of Lean service jobs, with real (non-Lean) child processes."""
from __future__ import annotations

import asyncio
import errno
import sys
import time
from pathlib import Path

import pytest

from formal_geyser import cgroups
from formal_geyser.lean_service.osjobs import JobGroup
from formal_geyser.process import ProcessRunner

TICKER = """
import os, pathlib, subprocess, sys, time
out = pathlib.Path(sys.argv[1])
staged = out.with_suffix(".tmp")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # A grandchild in the tree.
block = bytearray(160 * 2**20)  # Touched, so it is private and resident.
for i in range(0, len(block), 4096):
    block[i] = 1
count = 0
while True:
    count += 1
    try:  # Whole numbers only, even when suspended mid-write.
        staged.write_text(str(count))
        os.replace(staged, out)
    except OSError:
        pass  # The reader holds the file open; the next tick replaces it.
    time.sleep(0.05)
"""


async def _counter(path: Path) -> int:
    for _ in range(100):
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            await asyncio.sleep(0.05)
    raise AssertionError("the child never counted")


def test_a_cgroup_removed_while_its_processes_are_listed_is_skipped(tmp_path, monkeypatch):
    for name in ("ending", "kept", "gone"):
        (tmp_path / name).mkdir()

    def pids(cgroup):
        if cgroup.name == "ending":  # The kernel removes it: cgroup.procs is no longer readable.
            raise OSError(errno.ENODEV, "No such device")
        if cgroup.name == "gone":
            raise FileNotFoundError(errno.ENOENT, "No such file")
        return [7] if cgroup.name == "kept" else []
    monkeypatch.setattr(cgroups, "_pids", pids)
    assert cgroups.processes(tmp_path) == [7]

    def denied(cgroup):
        raise PermissionError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(cgroups, "_pids", denied)
    with pytest.raises(PermissionError):
        cgroups.processes(tmp_path)


async def test_a_group_suspends_resumes_measures_and_ends_its_whole_tree(tmp_path):
    group = JobGroup()
    counter = tmp_path / "count.txt"
    script = tmp_path / "ticker.py"
    script.write_text(TICKER, encoding="utf-8")
    run = asyncio.create_task(ProcessRunner().run([sys.executable, str(script), str(counter)], cwd=tmp_path,
                                                  group=group, register=False))
    try:
        await _counter(counter)
        for _ in range(100):
            usage = group.usage()
            if usage.private_memory >= 150 * 2**20:
                break
            await asyncio.sleep(0.1)
        assert usage.private_memory >= 150 * 2**20 and usage.peak_committed_memory >= 150 * 2**20
        assert usage.processes >= 2  # The child and its grandchild.
        assert group.suspend() >= 2
        await asyncio.sleep(0.3)
        frozen = await _counter(counter)
        await asyncio.sleep(0.6)
        assert await _counter(counter) == frozen
        cpu = group.usage().cpu_seconds
        group.resume()
        await asyncio.sleep(0.6)
        assert await _counter(counter) > frozen and group.usage().cpu_seconds >= cpu
        assert group.peak_private_memory >= 150 * 2**20
        group.terminate()
        result = await asyncio.wait_for(run, 30)
        assert result.returncode is not None and result.returncode != 0 and result.tree_quiet
        assert group.pids() == []  # The whole tree has ended, the grandchild included.
    finally:
        group.close()
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert not (tmp_path / ".task-based").exists()  # No TaskHub ownership receipt was written.


async def test_a_suspended_group_still_ends_when_terminated(tmp_path):
    group = JobGroup()
    run = asyncio.create_task(ProcessRunner().run([sys.executable, "-c", "import time; time.sleep(60)"],
                                                  cwd=tmp_path, group=group, register=False))
    try:
        for _ in range(100):
            if group.usage().processes >= 2:
                break
            await asyncio.sleep(0.05)
        group.suspend()
        group.terminate()
        assert (await asyncio.wait_for(run, 30)).tree_quiet
    finally:
        group.close()


async def test_a_group_takes_one_process_after_another_and_keeps_what_each_used(tmp_path):
    group = JobGroup()
    code = "block = bytearray(64 * 2**20)\nfor i in range(0, len(block), 4096): block[i] = 1\nsum(range(3 * 10**6))"
    try:
        for _ in range(3):
            result = await ProcessRunner().run([sys.executable, "-c", code], cwd=tmp_path, group=group, register=False)
            assert result.returncode == 0, result.stderr
        usage = group.usage()
        assert usage.processes == 0 and usage.peak_committed_memory >= 60 * 2**20 and usage.cpu_seconds > 0.1
    finally:
        group.close()
    assert not group.path.exists()


async def test_a_process_that_starts_while_its_group_is_suspended_waits_for_it(tmp_path):
    group = JobGroup()
    marker = tmp_path / "ran"
    try:
        group.suspend()
        run = asyncio.create_task(ProcessRunner().run([sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
                                                      cwd=tmp_path, group=group, register=False))
        await asyncio.sleep(1.0)
        assert not marker.exists() and not run.done()
        group.resume()
        assert (await asyncio.wait_for(run, 30)).returncode == 0 and marker.exists()
    finally:
        group.close()


async def test_time_spent_suspended_does_not_count_against_the_timeout(tmp_path):
    sleeper = [sys.executable, "-c", "import time; time.sleep(1.5)"]
    started = time.monotonic()
    plain = await ProcessRunner().run(sleeper, cwd=tmp_path, timeout=1.0, register=False)
    assert plain.timed_out
    paused = await ProcessRunner().run(sleeper, cwd=tmp_path, timeout=1.0, register=False,
                                       paused=lambda: min(1.0, time.monotonic() - started))
    assert not paused.timed_out and paused.returncode == 0
