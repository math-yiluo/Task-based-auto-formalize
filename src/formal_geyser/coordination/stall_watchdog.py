"""Write every thread's stack to the log when TaskHub stalls; no debugger and no root.

In the Linux pilot one admission took 12 minutes and nearly every worker went quiet meanwhile,
while the coordination thread was idle and the API still answered. py-spy could not attach
without root (`kernel.yama.ptrace_scope=1`), so this runs inside the service instead.

A stall is either of two signs: the event loop has not run a one-second timer for
`loop_seconds`, or a watched operation (an admission) has run for `operation_seconds`. Its
report gives the reasons, the default thread pool and the caller's description (the
coordination queue), then every thread's stack with its CPU use over one second, identical
stacks once, and, while the event loop still runs, each stalled operation's await chain and
all tasks counted by where they wait. A stall is reported when it starts and every
`repeat_seconds` while it lasts; its end is logged too.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOOP_SECONDS = 10.0
OPERATION_SECONDS = 120.0
REPEAT_SECONDS = 300.0
CHECK_SECONDS = 1.0
CPU_SAMPLE_SECONDS = 1.0
ANSWER_SECONDS = 5.0
STACK_FRAMES = 60
TASK_PLACES = 40


class Watch:
    """One watched operation. `detail` says what it is doing now, for the report."""

    __slots__ = ("name", "detail", "started", "task")

    def __init__(self, name: str, task: asyncio.Task | None):
        self.name, self.detail, self.started, self.task = name, "", time.monotonic(), task

    def describe(self, now: float) -> str:
        text = f"{self.name} has run for {now - self.started:.0f} s"
        return f"{text} ({self.detail})" if self.detail else text


_watches: dict[int, Watch] = {}
_watches_lock = threading.Lock()
_keys = itertools.count()


@contextmanager
def watched(name: str) -> Iterator[Watch]:
    """Mark an operation that a running watchdog reports once it has run too long."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    key, watch = next(_keys), Watch(name, task)
    with _watches_lock:
        _watches[key] = watch
    try:
        yield watch
    finally:
        with _watches_lock:
            _watches.pop(key, None)


def _thread_cpu() -> dict[int, float]:
    """CPU seconds each thread of this process has used, by native thread ID (Linux only)."""
    seconds: dict[int, float] = {}
    try:
        tick = os.sysconf("SC_CLK_TCK")
        entries = list(Path("/proc/self/task").iterdir())
    except (AttributeError, OSError, ValueError):
        return seconds
    for entry in entries:
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            seconds[int(entry.name)] = (int(fields[11]) + int(fields[12])) / tick
        except (OSError, ValueError, IndexError):
            continue
    return seconds


def await_chain(task: asyncio.Task) -> list[str]:
    """Where a task waits: its coroutines from the outermost in, then what the innermost awaits."""
    lines: list[str] = []
    awaitable: Any = task.get_coro()
    for _ in range(200):
        frame = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "gi_frame", None)
        if frame is None:
            if awaitable is not None:
                lines.append("awaiting " + type(awaitable).__name__)
            break
        code = frame.f_code
        lines.append(f'File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        awaitable = (awaitable.cr_await if hasattr(awaitable, "cr_await")
                     else getattr(awaitable, "gi_yieldfrom", None))
        if awaitable is None:
            break
    return lines


class StallWatchdog:
    def __init__(self, *, describe: Callable[[], list[str]] | None = None,
                 loop_seconds: float = LOOP_SECONDS, operation_seconds: float = OPERATION_SECONDS,
                 repeat_seconds: float = REPEAT_SECONDS, check_seconds: float = CHECK_SECONDS,
                 cpu_sample_seconds: float = CPU_SAMPLE_SECONDS, log: logging.Logger = logger):
        self.describe = describe
        self.loop_seconds, self.operation_seconds = loop_seconds, operation_seconds
        self.repeat_seconds, self.check_seconds = repeat_seconds, check_seconds
        self.cpu_sample_seconds = cpu_sample_seconds
        self.log = log
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread = ""
        self._beat = time.monotonic()
        self._timer: asyncio.TimerHandle | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stalled_since: float | None = None
        self._reported_at: float | None = None

    def start(self) -> None:
        """Start from the event loop's own thread."""
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.current_thread().name
        self._beat = time.monotonic()
        self._timer = self.loop.call_later(self.check_seconds, self._tick)
        self._thread = threading.Thread(target=self._run, name="taskhub-stall-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._timer is not None:
            self._timer.cancel()
        if self._thread is not None:
            # A report waits at most ANSWER_SECONDS for this (the loop's) thread.
            self._thread.join(timeout=ANSWER_SECONDS + self.cpu_sample_seconds + 5)

    def _tick(self) -> None:
        self._beat = time.monotonic()
        if not self._stop.is_set() and self.loop is not None:
            self._timer = self.loop.call_later(self.check_seconds, self._tick)

    def _run(self) -> None:
        while not self._stop.wait(self.check_seconds):
            try:
                self.check()
            except Exception:
                self.log.exception("The stall watchdog's check failed")

    def check(self, now: float | None = None) -> str | None:
        """Report a stall when one starts and every `repeat_seconds` while it lasts."""
        now = time.monotonic() if now is None else now
        reasons = []
        lag = now - self._beat
        loop_running = lag < self.loop_seconds
        if not loop_running:
            reasons.append(f"the event loop has not run for {lag:.0f} s")
        with _watches_lock:
            stalled = sorted((watch for watch in _watches.values() if now - watch.started >= self.operation_seconds),
                             key=lambda watch: watch.started)
        reasons.extend(watch.describe(now) for watch in stalled)
        if not reasons:
            if self._stalled_since is not None:
                self.log.warning("TaskHub stall ended after %.0f s", now - self._stalled_since)
                self._stalled_since = self._reported_at = None
            return None
        if self._stalled_since is None:
            self._stalled_since = now
        if self._reported_at is not None and now - self._reported_at < self.repeat_seconds:
            return None
        self._reported_at = now
        report = self.report(reasons, stalled, loop_running=loop_running)
        self.log.warning("%s", report)
        return report

    def report(self, reasons: list[str], stalled: list[Watch], *, loop_running: bool) -> str:
        lines = ["TaskHub stall: " + "; ".join(reasons) + ".", self._pool()]
        if self.describe is not None:
            try:
                lines.extend(self.describe())
            except Exception as error:
                lines.append(f"The service could not be described: {type(error).__name__}: {error}")
        try:
            lines.append("Load average: " + " ".join(f"{value:.2f}" for value in os.getloadavg()))
        except (AttributeError, OSError):
            pass
        lines.extend(self._threads())
        if not loop_running:
            lines.append(f"The event loop is blocked, so its tasks are not listed; its thread is {self.loop_thread}.")
        else:
            lines.extend(self._tasks_from_loop(stalled))
        return "\n".join(lines)

    def _pool(self) -> str:
        executor = getattr(self.loop, "_default_executor", None)
        if executor is None:
            return "Default thread pool: not started."
        try:
            threads = len(executor._threads)
            busy = threads - executor._idle_semaphore._value
            return (f"Default thread pool: {threads} of {executor._max_workers} threads, about {busy} busy, "
                    f"{executor._work_queue.qsize()} calls queued.")
        except AttributeError:
            return "Default thread pool: not readable."

    def _threads(self) -> list[str]:
        before = _thread_cpu()
        time.sleep(self.cpu_sample_seconds)
        after = _thread_cpu()
        frames = sys._current_frames()
        groups: dict[str, list[tuple[str, float | None]]] = {}
        for thread in threading.enumerate():
            frame = frames.get(thread.ident) if thread.ident is not None else None
            if frame is None or thread.ident == threading.get_ident():
                continue
            stack = "".join(traceback.format_stack(frame, limit=STACK_FRAMES)).rstrip("\n")
            native = thread.native_id
            cpu = (100 * (after[native] - before[native]) / self.cpu_sample_seconds
                   if native in before and native in after else None)
            groups.setdefault(stack, []).append((thread.name, cpu))
        lines = []
        for stack, members in sorted(groups.items(), key=lambda item: -max(cpu or 0.0 for _, cpu in item[1])):
            names = ", ".join(name if cpu is None else f"{name} ({cpu:.0f}% CPU)" for name, cpu in members)
            lines.append(("Threads " if len(members) > 1 else "Thread ") + names + ":")
            lines.append(stack)
        return lines

    def _tasks_from_loop(self, stalled: list[Watch]) -> list[str]:
        loop = self.loop
        if loop is None:
            return ["No event loop is watched."]
        done, lines = threading.Event(), []

        def collect() -> None:
            try:
                lines.extend(self._tasks(loop, stalled))
            except Exception as error:
                lines.append(f"The tasks could not be listed: {type(error).__name__}: {error}")
            finally:
                done.set()
        try:
            loop.call_soon_threadsafe(collect)
        except RuntimeError:
            return ["The event loop is closed."]
        if not done.wait(ANSWER_SECONDS):
            return [f"The event loop did not answer within {ANSWER_SECONDS:.0f} s, so its tasks are not listed."]
        return list(lines)

    @staticmethod
    def _tasks(loop: asyncio.AbstractEventLoop, stalled: list[Watch]) -> list[str]:
        lines = []
        for watch in stalled:
            if watch.task is not None and not watch.task.done():
                lines.append(f"Await chain of {watch.name} (task {watch.task.get_name()}):")
                lines.extend("  " + line for line in await_chain(watch.task))
        tasks = asyncio.all_tasks(loop)
        places: Counter[str] = Counter()
        for task in tasks:
            frames = [line for line in await_chain(task) if line.startswith("File ")]
            places[frames[-1] if frames else "(not started)"] += 1
        lines.append(f"{len(tasks)} tasks, by where they wait:")
        lines.extend(f"  {count:4d}  {place}" for place, count in places.most_common(TASK_PLACES))
        return lines
