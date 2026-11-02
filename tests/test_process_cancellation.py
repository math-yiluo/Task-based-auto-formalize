import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from formal_geyser.process import ProcessRunner


class DelayedStream:
    def __init__(self):
        self.reading = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_allowed = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    async def read(self, size):
        self.reading.set()
        try:
            await asyncio.Future()
        finally:
            self.cleanup_started.set()
            await self.cleanup_allowed.wait()
            self.cleanup_finished.set()


class ControlledHost:
    def __init__(self):
        self.stdout = DelayedStream()
        self.stderr = DelayedStream()
        self.stdin = SimpleNamespace(close=lambda: None)
        self.stop_started = asyncio.Event()
        self.stop_allowed = asyncio.Event()
        self.stop_finished = asyncio.Event()
        self.process = SimpleNamespace(returncode=None, wait=self.wait)

    async def wait(self):
        await self.stop_finished.wait()
        return 0

    async def stop(self, *, grace):
        self.stop_started.set()
        await self.stop_allowed.wait()
        self.process.returncode = 0
        self.stop_finished.set()
        return SimpleNamespace(tree_quiet=True, forced=False)


@pytest.mark.parametrize("second_cancellation", ["process-stop", "pipe-cleanup"])
async def test_repeated_cancellation_waits_for_process_and_pipe_cleanup(
    tmp_path, monkeypatch, second_cancellation,
):
    host = ControlledHost()

    async def start(*args, **kwargs):
        return host

    monkeypatch.setattr("formal_geyser.process.OwnedProcessHost.start", start)
    callback = asyncio.create_task(ProcessRunner().run(
        [str(Path(sys.executable).resolve())], cwd=tmp_path,
    ))
    try:
        await asyncio.wait_for(host.stdout.reading.wait(), timeout=1)
        await asyncio.wait_for(host.stderr.reading.wait(), timeout=1)
        callback.cancel()
        await asyncio.wait_for(host.stop_started.wait(), timeout=1)
        if second_cancellation == "process-stop":
            callback.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not callback.done(), "Callback finished before its owned process stopped"
        host.stop_allowed.set()
        await asyncio.wait_for(host.stdout.cleanup_started.wait(), timeout=1)
        await asyncio.wait_for(host.stderr.cleanup_started.wait(), timeout=1)
        if second_cancellation == "pipe-cleanup":
            callback.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert host.stop_finished.is_set()
        assert not callback.done(), "Callback finished before pipe cleanup completed"
        host.stdout.cleanup_allowed.set()
        await asyncio.wait_for(host.stdout.cleanup_finished.wait(), timeout=1)
        assert not callback.done(), "Callback did not wait for both pipes"
        host.stderr.cleanup_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(callback, timeout=1)
        assert host.stdout.cleanup_finished.is_set()
        assert host.stderr.cleanup_finished.is_set()
    finally:
        host.stop_allowed.set()
        host.stdout.cleanup_allowed.set()
        host.stderr.cleanup_allowed.set()
        if not callback.done() and not callback.cancelling():
            callback.cancel()
        await asyncio.wait_for(asyncio.gather(callback, return_exceptions=True), timeout=1)
        await asyncio.sleep(0)
