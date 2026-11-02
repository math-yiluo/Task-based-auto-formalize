"""The stall watchdog writes thread stacks and await chains to the log, without a debugger."""

import asyncio
import logging
import threading
import time

from formal_geyser.coordination import stall_watchdog
from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import ProjectConfig
from formal_geyser.coordination.stall_watchdog import StallWatchdog, watched
from tests.coordination.test_collaborative_core import command

LOGGER = "formal_geyser.coordination.stall_watchdog"


def messages(caplog):
    return [record.getMessage() for record in caplog.records]


async def test_a_long_operation_is_reported_once_with_its_await_chain_until_it_ends(caplog):
    watchdog = StallWatchdog(describe=lambda: ["Coordination database: 3 calls queued or running."],
                             loop_seconds=3600, operation_seconds=60, check_seconds=3600,
                             cpu_sample_seconds=0.01)
    watchdog.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled_admission():
        with watched("admission") as watch:
            watch.detail = "preparing the workspace of worker w-59"
            entered.set()
            await release.wait()

    task = asyncio.create_task(stalled_admission(), name="admission-under-test")
    try:
        await entered.wait()
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert await asyncio.to_thread(watchdog.check, started + 30) is None
            report = await asyncio.to_thread(watchdog.check, started + 61)
            assert await asyncio.to_thread(watchdog.check, started + 62) is None  # Once per stall.
            release.set()
            await task
            assert await asyncio.to_thread(watchdog.check, started + 63) is None
    finally:
        release.set()
        watchdog.stop()
    assert report.startswith("TaskHub stall: admission has run for 6")
    assert "(preparing the workspace of worker w-59)." in report.splitlines()[0]
    assert "Default thread pool:" in report and "Coordination database: 3 calls queued or running." in report
    assert "MainThread" in report
    chain = report[report.index("Await chain of admission (task admission-under-test):"):]
    assert "in stalled_admission" in chain and "in wait" in chain
    assert "tasks, by where they wait:" in report
    assert messages(caplog)[0] == report
    assert messages(caplog)[1].startswith("TaskHub stall ended after")


async def test_a_blocked_event_loop_is_reported_from_the_watchdog_thread(caplog):
    watchdog = StallWatchdog(loop_seconds=0.3, check_seconds=0.05, cpu_sample_seconds=0.01)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        watchdog.start()
        try:
            time.sleep(1.0)  # Blocks the event loop on purpose.
            for _ in range(100):
                await asyncio.sleep(0.05)
                if any(message.startswith("TaskHub stall ended") for message in messages(caplog)):
                    break
        finally:
            watchdog.stop()
    reports = [message for message in messages(caplog) if message.startswith("TaskHub stall:")]
    assert len(reports) == 1
    assert reports[0].startswith("TaskHub stall: the event loop has not run for")
    assert "The event loop is blocked, so its tasks are not listed; its thread is MainThread." in reports[0]
    assert "time.sleep(1.0)" in reports[0]  # The loop thread's stack shows what blocks it.
    assert any(message.startswith("TaskHub stall ended after") for message in messages(caplog))


async def test_admission_is_watched_while_it_runs(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        run = await command(hub, "run.start")
        seen = []

        async def prepare(result, *, tolerate_failure=False):
            with stall_watchdog._watches_lock:
                seen.extend((watch.name, watch.detail) for watch in stall_watchdog._watches.values())
            return result

        monkeypatch.setattr(hub, "prepare_assignment_workspace", prepare)
        assert await hub.next_assignment(run["id"]) is None
        assert seen == [("admission", "selecting")]
        assert not stall_watchdog._watches
    finally:
        await hub.close()


async def test_the_service_runs_the_watchdog_only_while_it_serves(tmp_path):
    initialize(tmp_path, ProjectConfig())

    def running():
        return any(thread.name == "taskhub-stall-watchdog" for thread in threading.enumerate())

    app = create_app(tmp_path, stall_watchdog=True)
    async with app.router.lifespan_context(app):
        assert running()
    assert not running()
    plain = create_app(tmp_path)
    async with plain.router.lifespan_context(plain):
        assert not running()
