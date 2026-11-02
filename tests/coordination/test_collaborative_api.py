"""Armed service drivers observe collaborative coordination commands."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.models import ProjectConfig


@pytest.mark.parametrize("name", [
    "task.join", "task.invitation.update", "task.invitation.withdraw", "task.revise",
    "artifact.stage", "artifact.submit", "review.accept", "review.reject", "task.pause",
    "task.complete.cancel",
])
async def test_collaborative_commands_wake_only_an_armed_driver(tmp_path, name):
    initialize(tmp_path, ProjectConfig())
    drives = 0

    class Hub:
        config = SimpleNamespace(name="Test", domain="general")

        async def run(self, run_id):
            return {"id": run_id, "state": "running"}

        async def mark_run_resumable(self, run_id):
            pass

        async def execute(self, command, actor):
            return {"command": command.name}

        async def subscriptions(self, worker_id):
            return {"worker_id": worker_id, "tasks": []}

    class Runner:
        def __init__(self, hub):
            pass

        async def run_until_idle(self, run_id, **_):
            nonlocal drives
            drives += 1
            return {"steps": 0}

    app = create_app(tmp_path, hub=Hub(), runner_factory=Runner)

    async def drain():
        for _ in range(100):
            driver = app.state.driver.passes.get("run")
            if driver is None or driver.done():
                return
            await asyncio.sleep(0.001)
        raise AssertionError("The scripted driver did not finish")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            command = {"name": name, "key": uuid4().hex, "payload": {}}
            assert (await client.post("/v1/commands", json=command)).status_code == 200
            await drain()
            assert drives == 0
            assert (await client.post("/v1/runs/run/drive")).status_code == 202
            await drain()
            assert drives == 1
            command["key"] = uuid4().hex
            assert (await client.post("/v1/commands", json=command)).status_code == 200
            await drain()
            assert drives == 2
            path = "/v1/workers/worker/subscriptions"
            assert (await client.get(path)).json() == {
                "worker_id": "worker", "tasks": []}
