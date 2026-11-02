"""Where the Lean service spends its time, for an operator looking at a slow service.

The service's event loop serves every check and stalled for 10 to 30 s in the Linux pilot. CPU
by thread and code (coordination.cpu_profile) shows what is busy; where the event loop's thread
is, sampled by wall-clock time, also shows what it waits in when it is not running Python.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..coordination.cpu_profile import MAX_SECONDS, _project_frames, profile


def _innermost(frame) -> str:
    code = frame.f_code
    return f"{Path(code.co_filename).name}:{code.co_name}"


def loop_wall_time(seconds: float, interval: float = 0.01) -> dict:
    """Where the main thread (the event loop's) is, by share of wall-clock samples: its innermost
    frame, and its innermost frame in this project."""
    main = threading.main_thread().ident
    frames: Counter[str] = Counter()
    project: Counter[str] = Counter()
    samples = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = sys._current_frames().get(main) if main is not None else None
        if frame is not None:
            samples += 1
            frames[_innermost(frame)] += 1
            project[_project_frames(frame)[0]] += 1
        time.sleep(interval)

    def shares(counter: Counter[str]) -> list[dict]:
        return [{"name": name, "share": round(count / samples, 3)} for name, count in counter.most_common(25)]
    return {"samples": samples, "innermost": shares(frames) if samples else [],
            "innermost_in_project": shares(project) if samples else []}


def diagnose(seconds: float) -> dict:
    """CPU by thread and code, and the event loop thread's wall-clock whereabouts, together."""
    if not 0 < seconds <= MAX_SECONDS:
        raise ValueError(f"Profile between 0 and {MAX_SECONDS:.0f} seconds")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lean-diagnostics") as pool:
        cpu = pool.submit(profile, seconds)
        loop = pool.submit(loop_wall_time, seconds)
        return {"cpu": cpu.result(), "event_loop": loop.result()}
