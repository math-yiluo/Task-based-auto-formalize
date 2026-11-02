"""The dashboard's reads never wait on the coordination thread or re-verify immutable files."""
from __future__ import annotations

import asyncio
import stat
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from tests.coordination.test_communications import WORKER, CommunicationHarness

OPERATOR = Actor(role=Role.OPERATOR)


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), OPERATOR)


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    task = await command(hub, "task.publish", {"contract": {"title": "Dashboard task", "objective": "Show it."}})
    await command(hub, "run.start")
    try:
        yield hub, task
    finally:
        await hub.close()


async def test_overview_reads_answer_while_the_coordination_thread_is_busy(project):
    hub, _ = project
    release = threading.Event()
    blocked = asyncio.create_task(hub.db.call(lambda c: release.wait(30)))
    await asyncio.sleep(0.05)
    try:
        reads = await asyncio.wait_for(asyncio.gather(
            hub.task_overview(), hub.task_outline(), hub.list_workers(), hub.list_runs(),
            hub.list_human_requests(), hub.integration_queue(), hub.delivery_holds(),
            hub.latest_operator_messages(), hub.repository_status()), timeout=5)
    finally:
        release.set()
        await blocked
    assert [item["contract"]["title"] for item in reads[1]["items"]] == ["Dashboard task"]
    assert reads[0]["task_count"] == 1


async def test_summaries_use_the_ledger_contract_while_opening_a_task_still_verifies_files(project):
    hub, task = project
    opened = await hub.task(task["id"])
    reference = opened["files"]
    target = Path(reference["root"]) / "objective.md"
    target.chmod(stat.S_IREAD | stat.S_IWRITE)
    target.write_bytes(b"Unexpected external edit.\n")
    outline = await hub.task_outline()
    assert outline["items"][0]["contract"] == {"title": "Dashboard task", "kind": opened["contract"]["kind"]}
    with pytest.raises(Exception):  # The authoritative task read rejects the changed file.
        await hub.task(task["id"])


async def test_request_documents_are_read_once_per_recorded_identity(tmp_path, monkeypatch):
    comm = CommunicationHarness(tmp_path)

    def seed(c):
        c.execute("INSERT INTO runs(id,state,limits_json,created_at) VALUES('run','running','{}',0)")
        c.execute("INSERT INTO tasks(id,current_revision,state,created_at) VALUES('task',1,'claimed',0)")
        c.execute("INSERT INTO workers(id,state,active_attempt,created_at) VALUES('alice','running','attempt-alice',0)")
        c.execute("INSERT INTO attempts(id,task_id,task_revision,worker_id,run_id,fencing_token,state,created_at) "
                  "VALUES('attempt-alice','task',1,'alice','run',1,'running',0)")
    await comm.db.call(seed, write=True)
    try:
        await comm.execute("message.request", {"request_key": "scope", "title": "Clarify scope",
                                                "text": "Which scope?"}, WORKER)
        reads = []
        original = comm._read_document
        monkeypatch.setattr(comm, "_read_document", lambda row: reads.append(row["id"]) or original(row))
        first = await comm.list_human_requests()
        second = await comm.list_human_requests()
        assert first == second and first[0]["title"] == "Clarify scope" and len(reads) == 1
        # Callers cannot change the cached document.
        first[0]["title"] = "Changed by a caller"
        assert (await comm.list_human_requests())[0]["title"] == "Clarify scope"
    finally:
        await comm.db.close()


def test_overview_reports_a_stopped_domain_service_as_stopped(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())

    class Plugin:
        async def resource_status(self, hub):
            return None  # What the Lean plugin reads while its service is stopped.

    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/v1/overview?compact=true").json()["lean_resources"] is None  # No such service.
        monkeypatch.setattr("formal_geyser.coordination.plugins.load_plugin", lambda domain: Plugin())
        assert client.get("/v1/status").json()["lean_resources"] == {"running": False}


def test_overview_answers_unchanged_polls_with_304_and_omits_profiles(tmp_path):
    initialize(tmp_path, ProjectConfig())
    with TestClient(create_app(tmp_path)) as client:
        first = client.get("/v1/overview?compact=true")
        assert first.status_code == 200 and first.headers["etag"].startswith('"')
        assert first.json()["task_count"] == 0
        assert all("profile_json" not in worker for worker in first.json()["workers"])
        again = client.get("/v1/overview?compact=true", headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
        stale = client.get("/v1/overview?compact=true", headers={"If-None-Match": '"stale"'})
        steady = lambda value: {key: item for key, item in value.items() if key != "lean_resources"}  # noqa: E731
        assert stale.status_code == 200 and steady(stale.json()) == steady(first.json())


def test_the_compact_overview_leaves_retired_workers_out_and_is_shared_for_a_few_seconds(tmp_path, monkeypatch):
    # In the Linux pilot each poll, every 2 s per tab, built 7 MB in 1.3-4.5 s, half of it for 3,424
    # retired workers and the 3,700 tasks they still referred to.
    initialize(tmp_path, ProjectConfig())
    with TestClient(create_app(tmp_path)) as client:
        hub = client.app.state.hub
        register = lambda: client.post(  # noqa: E731
            "/v1/commands", json={"name": "worker.register", "key": uuid4().hex, "payload": {}}).json()
        live, gone = register(), register()
        retired_task = client.post("/v1/commands", json={"name": "task.publish", "key": uuid4().hex, "payload": {
            "contract": {"title": "Reviewed long ago", "objective": "Keep it."}}}).json()
        client.portal.call(hub.db.write, lambda c: c.execute(
            "UPDATE workers SET state='retired',work_intention='retired',current_task_id=? WHERE id=?",
            (retired_task["id"], gone["id"])))
        builds = []
        original = hub.list_workers

        async def counted(**options):
            builds.append(options)
            return await original(**options)
        monkeypatch.setattr(hub, "list_workers", counted)

        compact = client.get("/v1/overview?compact=true").json()
        assert [item["id"] for item in compact["workers"]] == [live["id"]] and compact["retired_count"] == 1
        assert retired_task["id"] not in {item["id"] for item in compact["tasks"]}
        assert client.get("/v1/retired-workers").json() == {
            "count": 1, "workers": [[gone["id"], "Worker 1", "ordinary", "codex"]]}
        assert {item["id"] for item in client.get("/v1/overview").json()["workers"]} == {live["id"], gone["id"]}
        # Shared until it is OVERVIEW_SECONDS old, and built again at once after an operator command.
        client.get("/v1/overview?compact=true")
        assert builds == [{"retired": False}, {"retired": True}]
        newer = register()
        assert newer["id"] in {item["id"] for item in client.get("/v1/overview?compact=true").json()["workers"]}
        assert builds[-1] == {"retired": False} and len(builds) == 3
        monkeypatch.setattr("formal_geyser.coordination.api.OVERVIEW_SECONDS", 0)
        client.get("/v1/overview?compact=true")
        assert len(builds) == 4
