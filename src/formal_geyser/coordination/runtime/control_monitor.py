"""One read of the coordination database for what every turn and validation under way checks."""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class ControlUnavailable(RuntimeError):
    """A control read failed, or found no such run or worker."""


class ControlMonitor:
    """A turn under way watches its run's control, its own binding and its inbox; a validation,
    its run's control. Read by each on its own every half second, that was some forty statements
    a conversation, thousands a second with many conversations, and every statement and row gives
    up the interpreter lock and waits behind busy threads to take it back. The monitor reads every
    watch at once each interval (Hub.activation_controls), and only while something watches."""

    def __init__(self, hub, interval: float):
        self.hub = hub
        self.interval = interval
        self._watches: Counter[tuple[str, str | None]] = Counter()
        self._started = 0  # Reads begun; each view comes from one of them.
        self._latest: tuple[int, dict | Exception] = (0, {})
        self._published = asyncio.Condition()
        self._reading: asyncio.Task | None = None

    @asynccontextmanager
    async def watch(self, run_id: str, worker_id: str | None) -> AsyncIterator[ControlWatch]:
        """Watch a run as it applies to a worker, or the run alone, while the block runs."""
        key = (run_id, worker_id)
        self._watches[key] += 1
        if self._reading is None or self._reading.done():
            self._reading = asyncio.create_task(self._read_while_watched(), name="control-monitor")
        try:
            yield ControlWatch(self, key)
        finally:
            self._watches[key] -= 1
            if self._watches[key] <= 0:
                del self._watches[key]

    async def _read_while_watched(self) -> None:
        while self._watches:
            await asyncio.sleep(self.interval)
            if not self._watches:
                return
            self._started += 1
            number = self._started
            result: dict | Exception
            try:
                result = await self.hub.activation_controls(list(self._watches))
            except Exception as error:
                # Each watcher decides what an unreadable control means for it.
                result = error
            async with self._published:
                self._latest = (number, result)
                self._published.notify_all()


class ControlWatch:
    """One watch's views, each from a read begun after the watch's previous look."""

    def __init__(self, monitor: ControlMonitor, key: tuple[str, str | None]):
        self.monitor, self.key = monitor, key
        self._began = self._after = monitor._started

    async def next(self) -> dict[str, Any]:
        """The view from the first read begun after the previous call returned, or after the
        watch began, so that it reflects whatever the watcher saw or did before asking."""
        monitor = self.monitor
        async with monitor._published:
            await monitor._published.wait_for(lambda: monitor._latest[0] > self._after)
            result = monitor._latest[1]
        self._after = monitor._started
        return self._view(result)

    def latest(self) -> dict[str, Any] | None:
        """The newest view read since the watch began, without waiting; None before the first."""
        number, result = self.monitor._latest
        return self._view(result) if number > self._began else None

    def _view(self, result: dict | Exception) -> dict[str, Any]:
        if isinstance(result, Exception):
            raise ControlUnavailable(
                f"Run control could not be read: {type(result).__name__}: {result}") from result
        view = result.get(self.key)
        if view is None:
            raise ControlUnavailable(f"No run {self.key[0]} or worker {self.key[1]} to watch")
        return view
