"""The Lean service as a process: its HTTP API, its supervisor and the client that starts it."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.models import LeanServiceConfig, ProjectConfig
from formal_geyser.lean_service import client, server
from formal_geyser.lean_service.control import control
from formal_geyser.lean_service.scheduler import GIB
from formal_geyser.lean_service.service import LeanService
from tests.conftest import FakeRunner
from tests.test_lean_service import spec


def never_start(root):
    raise AssertionError("This test's service runs in the test")


async def serving(service, root):
    def ready():
        state = client.state_directory(root)
        state.mkdir(parents=True, exist_ok=True)
        (state / "service.json").write_text(json.dumps({"port": port, "instance": service.instance}), encoding="utf-8")

    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    app = server.create_app(service, on_ready=ready)
    instance = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    app.state.server = instance
    task = asyncio.create_task(instance.serve(sockets=[listener]))
    for _ in range(200):
        if instance.started:
            return instance, task
        await asyncio.sleep(0.05)
    raise AssertionError("The test server did not start")


async def test_the_http_api_runs_checks_for_local_operators_only(proof_case, monkeypatch):
    hub, _, artifact = proof_case
    service = LeanService(hub.root, LeanServiceConfig(), runner=FakeRunner(), measure=lambda: (64 * GIB, 128 * GIB),
                          threads=4)
    instance, task = await serving(service, hub.root)
    try:
        remote = client.HttpClient(hub.root, start=never_start)
        assert await remote.healthy()
        result = await client.run(remote, spec(hub, snapshot=artifact["snapshot"]))
        assert result["compilation"]["ok"] and result["definitive"]
        status = await remote.status()
        assert status["counts"] == {"running": 0, "queued": 0, "suspended": 0} and status["sandbox"] == "none"
        assert (await remote.telemetry(1))["lanes"]["official"]["jobs"] == 1
        with pytest.raises(KeyError):
            await remote.wait("unknown", 0)
        monkeypatch.setattr(server, "operator_request", lambda request, token: False)
        with pytest.raises(client.ServiceUnavailable, match="operator token"):
            await remote.status()
    finally:
        instance.should_exit = True
        await asyncio.wait_for(task, 30)


def wait_until(predicate, seconds=90.0, state=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    found = {}
    for name in ("supervisor.json", "service.json", "supervisor-events.jsonl", "service.log", "stop"):
        path = state / name if state is not None else None
        if path is not None and path.exists():
            found[name] = path.read_text(encoding="utf-8", errors="replace")[-2000:]
    raise AssertionError("Timed out; " + json.dumps(found))


async def test_the_service_starts_on_demand_outlives_its_client_restarts_and_stops(tmp_path):
    initialize(tmp_path, ProjectConfig(domain="lean", lean_service=LeanServiceConfig(sandbox="none")))
    state = client.state_directory(tmp_path)
    remote = client.HttpClient(tmp_path)  # Starts the real, detached supervisor.
    try:
        await remote.ensure_running()
        first = json.loads((state / "service.json").read_text(encoding="utf-8"))
        supervisor = json.loads((state / "supervisor.json").read_text(encoding="utf-8"))
        assert supervisor["pid"] != os.getpid() and first["pid"] != supervisor["pid"]
        os.kill(first["pid"], signal.SIGTERM)  # The server dies; its supervisor starts it again.

        def restarted():
            try:
                record = json.loads((state / "service.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return record if record["instance"] != first["instance"] else None

        second = await asyncio.to_thread(wait_until, restarted, 90.0, state)
        assert await remote.healthy()
        assert json.loads((state / "supervisor.json").read_text(encoding="utf-8"))["pid"] == supervisor["pid"]
        restart = await control(tmp_path, "restart")
        assert restart["running"] and restart["previous_instance"] == second["instance"]
        stopped = await control(tmp_path, "stop")
        assert stopped == {"running": False, "stopped_instance": restart["instance"]}
        await asyncio.to_thread(wait_until, lambda: not (state / "supervisor.json").exists(), 60, state)
        events = [json.loads(line) for line in (state / "supervisor-events.jsonl").read_text().splitlines()]
        assert [event["event"] for event in events] == ["server_exited"] * 3
        assert (await control(tmp_path, "status")) == {"running": False}
    finally:
        try:
            await control(tmp_path, "stop")
        except Exception:
            pass
        for name in ("supervisor.json", "service.json"):
            try:
                os.kill(json.loads((state / name).read_text(encoding="utf-8"))["pid"], signal.SIGTERM)
            except (OSError, ValueError, KeyError):
                pass
