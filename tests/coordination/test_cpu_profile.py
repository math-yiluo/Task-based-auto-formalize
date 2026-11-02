"""The CPU profile charges each thread's own CPU time to the code it runs."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.cpu_profile import profile
from formal_geyser.coordination.models import ProjectConfig
from formal_geyser.coordination.storage import canonical


def test_a_busy_thread_is_charged_to_the_project_code_it_runs():
    stop = threading.Event()
    value = {"items": [{"name": str(number), "value": number} for number in range(2000)]}

    def burn():
        while not stop.is_set():
            canonical(value)
    burner = threading.Thread(target=burn, name="burner")
    burner.start()
    try:
        report = profile(0.5, interval=0.005)
    finally:
        stop.set()
        burner.join()
    threads = {item["name"]: item["cpu_seconds"] for item in report["threads"]}
    assert threads.get("burner", 0) > 0.1
    functions = {item["name"] for item in report["functions"]}
    assert "burner storage.py:canonical" in functions
    with pytest.raises(ValueError):
        profile(0)


def test_the_service_profiles_itself_on_request(tmp_path):
    initialize(tmp_path, ProjectConfig())
    with TestClient(create_app(tmp_path)) as client:
        report = client.get("/v1/diagnostics/cpu", params={"seconds": 0.2}).json()
        assert report["seconds"] >= 0.2 and isinstance(report["threads"], list)
        assert client.get("/v1/diagnostics/cpu", params={"seconds": 1000}).status_code >= 400
