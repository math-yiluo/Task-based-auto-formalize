"""Small process-ownership receipts for automatic recovery after service death."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ... import cgroups
from ..storage import atomic_json


def _started(pid: int) -> int | None:
    """When a process started, in clock ticks since boot: with its PID, it names one process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def planned_receipt(cgroup: Path) -> dict:
    """What recovery probes for a process tree that may have started in `cgroup`."""
    return {"format": 2, "cgroup": cgroups.relative(cgroup), "owner_pid": os.getpid(),
            "owner_started": _started(os.getpid())}


def register_tree(cwd: Path, cgroup: Path) -> Path | None:
    # Paid sessions always run inside the Hub's per-worker workspace.
    # The runner binds this receipt to an attempt before sending a model input.
    state = next((p for p in (cwd, *cwd.parents) if p.name == ".task-based"), None)
    if state is None:
        return None
    directory = state / "owned-processes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (cgroup.name + ".json")
    atomic_json(path, {**planned_receipt(cgroup), "process_quiet": False, "created_at": time.time()})
    return path


def record_quiet(path: Path | None):
    if path is None:
        return
    record = json.loads(path.read_text(encoding="utf-8"))
    atomic_json(path, {**record, "process_quiet": True, "closed_at": time.time()})


def tree_quiet(record, *, end_orphan: bool = False) -> bool:
    """Whether a receipt's process tree has ended: its unique cgroup is empty or gone. Never
    inferred from elapsed time. With `end_orphan`, a tree whose owner has died is ended first,
    as its owner's unit would have ended it."""
    if not isinstance(record, dict) or record.get("format") != 2:
        return False
    cgroup = cgroups.located(record.get("cgroup"))
    if cgroup is None:
        return False
    if not cgroups.populated(cgroup):
        return True
    owner, started = record.get("owner_pid"), record.get("owner_started")
    if not end_orphan or type(owner) is not int or type(started) is not int or _started(owner) == started:
        return False
    cgroups.kill(cgroup)
    deadline = time.monotonic() + 10
    while cgroups.populated(cgroup) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not cgroups.populated(cgroup)
