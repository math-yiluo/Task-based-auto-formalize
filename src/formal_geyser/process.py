from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from formal_geyser.coordination.runtime.process import (
    OwnedProcessHost,
    ProcessHostError,
)

from .coordination.storage import collected


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_limited: bool = False
    error: str | None = None
    tree_quiet: bool = True

    @property
    def ok(self) -> bool:
        return (self.returncode == 0 and not self.timed_out and
                not self.output_limited and self.error is None and self.tree_quiet)

    def to_dict(self) -> dict:
        return asdict(self)


def process_environment(search_paths: Sequence[Path] = ()) -> dict[str, str]:
    """Exclude inherited Lean/Lake overrides; subprocesses still need OS runtime settings."""
    selected = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LD_LIBRARY_PATH")
    result = {key: value for key, value in os.environ.items() if key in selected}
    result["LEAN_PATH"] = os.pathsep.join(str(path.resolve()) for path in search_paths)
    result["NO_COLOR"] = "1"
    return result


class ProcessRunner:
    """Own the process tree with bounded output and an optional explicit time limit."""

    async def run(
        self, argv: Sequence[str], *, cwd: Path, timeout: float | None = None,
        output_limit: int = 1048576, env: Mapping[str, str] | None = None,
        stdin: bytes | None = None, group=None, register: bool = True,
        paused: Callable[[], float] | None = None,
    ) -> ProcessResult:
        """`group`, `register`: see OwnedProcessHost.start. `paused` returns the seconds the
        process has spent suspended so far, which do not count against `timeout`."""
        if not argv or not Path(argv[0]).is_absolute():
            raise ValueError("A subprocess requires an absolute executable path")
        if (timeout is not None and timeout <= 0) or output_limit < 1:
            raise ValueError("Process limits must be positive")
        start = time.monotonic()
        try:
            host = await OwnedProcessHost.start(
                argv, cwd=cwd, env=env if env is not None else process_environment(),
                max_frame_bytes=min(output_limit, 1048576), startup_timeout=60.0 if timeout is None else min(timeout, 60.0),
                group=group, register=register,
            )
        except (OSError, ProcessHostError) as error:
            elapsed = time.monotonic() - start
            return ProcessResult(tuple(argv), None, "", "", elapsed,
                                 timed_out=timeout is not None and elapsed >= timeout, error=str(error))
        process = host.process
        chunks: list[bytearray] = [bytearray(), bytearray()]
        total = 0
        overflow = asyncio.Event()

        async def read(stream, index):
            nonlocal total
            while data := await stream.read(8192):
                available = max(0, output_limit - total)
                chunks[index].extend(data[:available])
                total += len(data)
                if total > output_limit:
                    overflow.set()

        async def feed():
            try:
                if stdin is not None:
                    host.stdin.write(stdin)
                    await host.stdin.drain()
                host.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        pipe_tasks = [
            asyncio.create_task(read(host.stdout, 0)),
            asyncio.create_task(read(host.stderr, 1)),
            asyncio.create_task(feed()),
            asyncio.create_task(process.wait()),
        ]

        async def collect():
            await asyncio.gather(*pipe_tasks)

        combined = asyncio.create_task(collect())
        exceeded = asyncio.create_task(overflow.wait())

        async def cleanup() -> None:
            try:
                await host.stop(grace=0.1)
            finally:
                for task in [*pipe_tasks, exceeded]:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                await asyncio.gather(combined, exceeded, *pipe_tasks, return_exceptions=True)

        timed_out = False
        tree_quiet = False
        process_error = None
        def left() -> float | None:
            if timeout is None:
                return None
            return timeout - (time.monotonic() - start - (paused() if paused is not None else 0.0))

        try:
            while True:
                remaining = left()
                done, _ = await asyncio.wait({combined, exceeded},
                                             timeout=None if remaining is None else max(0.001, remaining),
                                             return_when=asyncio.FIRST_COMPLETED)
                remaining = left()
                if done or remaining is None or remaining <= 0:
                    break  # Time spent suspended extends the limit.
            timed_out = not done
            exit_result = await host.stop(grace=0.1)
            tree_quiet = exit_result.tree_quiet
            if exit_result.forced and not timed_out and not overflow.is_set():
                process_error = "Subprocess left running descendants after completion"
            try:
                await asyncio.wait_for(combined, timeout=2)
            except TimeoutError:
                combined.cancel()
                process_error = "Subprocess pipe draining did not complete"
        finally:
            # Cancellation must not let the caller remove a still-active workspace.
            await collected(asyncio.create_task(cleanup()))
        return ProcessResult(
            tuple(argv), process.returncode, chunks[0].decode("utf-8", errors="replace"),
            chunks[1].decode("utf-8", errors="replace"), time.monotonic() - start,
            timed_out=timed_out, output_limited=overflow.is_set(), error=process_error,
            tree_quiet=tree_quiet,
        )
