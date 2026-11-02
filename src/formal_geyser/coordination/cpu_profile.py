"""Where this process spends CPU, by thread and code, over a short window.

py-spy cannot attach to the service without root while `kernel.yama.ptrace_scope` is 1, and the
stall watchdog sees only moments of stalls. Here every thread's own CPU clock is read every few
milliseconds and the time it used since is charged to the stack it is in: the innermost frame in
this project's code (where the time goes) and the outermost (which operation it serves). Under
the interpreter lock, CPU a thread uses is time the coordination thread may wait for it.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

PROJECT = "/formal_geyser/"
MAX_SECONDS = 300.0


def _project_frames(frame) -> tuple[str, str]:
    """The innermost and outermost project frames of a stack, as `file:function`."""
    inner = outer = None
    while frame is not None:
        code = frame.f_code
        if PROJECT in code.co_filename:
            label = f"{Path(code.co_filename).name}:{code.co_name}"
            inner = inner or label
            outer = label
        frame = frame.f_back
    return inner or "(outside the project)", outer or "(outside the project)"


def profile(seconds: float, interval: float = 0.01) -> dict:
    """Sample for `seconds` (at most MAX_SECONDS); the CPU seconds of each thread family, each
    innermost project function and each outermost project operation, largest first."""
    if not 0 < seconds <= MAX_SECONDS:
        raise ValueError(f"Profile between 0 and {MAX_SECONDS:.0f} seconds")
    own = threading.get_ident()
    clocks: dict[int, int] = {}
    used: dict[int, float] = {}
    threads: defaultdict[str, float] = defaultdict(float)
    functions: defaultdict[str, float] = defaultdict(float)
    operations: defaultdict[str, float] = defaultdict(float)
    started, deadline = time.monotonic(), time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(interval)
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            if ident == own:
                continue
            try:
                clock = clocks.get(ident)
                if clock is None:
                    clock = clocks[ident] = time.pthread_getcpuclockid(ident)
                now = time.clock_gettime(clock)
            except (OSError, ValueError, OverflowError):
                continue  # The thread ended.
            delta, used[ident] = now - used.get(ident, now), now
            if delta <= 0:
                continue
            family = re.sub(r"_\d+$", "", names.get(ident, "unknown"))
            inner, outer = _project_frames(frame)
            threads[family] += delta
            functions[f"{family} {inner}"] += delta
            operations[f"{family} {outer}"] += delta
    elapsed = time.monotonic() - started

    def top(seconds_by_name: dict[str, float], count: int) -> list[dict]:
        largest = sorted(seconds_by_name.items(), key=lambda item: -item[1])[:count]
        return [{"name": name, "cpu_seconds": round(value, 3)} for name, value in largest]
    return {"seconds": round(elapsed, 1), "cpu_seconds": round(sum(threads.values()), 2),
            "threads": top(threads, 20), "functions": top(functions, 40), "operations": top(operations, 40)}
