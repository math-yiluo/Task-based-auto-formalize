"""The Lean service reports its CPU by thread and where its event loop's thread waits."""
from __future__ import annotations

import threading
import time

import pytest

from formal_geyser.lean_service.diagnostics import diagnose, loop_wall_time


def test_the_main_threads_wall_time_shows_where_it_waits():
    report: dict = {}
    sampler = threading.Thread(target=lambda: report.update(loop_wall_time(0.3, interval=0.005)))
    sampler.start()
    time.sleep(0.4)  # The main thread sleeps here throughout the window.
    sampler.join()
    assert report["samples"] > 10
    # Sleeping runs no Python frame of its own: this test function is innermost.
    assert report["innermost"][0]["name"] == (
        "test_lean_diagnostics.py:test_the_main_threads_wall_time_shows_where_it_waits")


def test_diagnose_reports_cpu_and_the_event_loop_together():
    report = diagnose(0.2)
    assert {"cpu", "event_loop"} <= report.keys()
    assert report["cpu"]["seconds"] == 0.2 and report["event_loop"]["samples"] > 0
    with pytest.raises(ValueError):
        diagnose(0)
