"""Operator file/talk inspection is authenticated and never starts model work."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.cli import app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    RepositoryConfig,
    Role,
)
from tests.coordination.publication_helpers import stage_note


class ReadHub:
    config = ProjectConfig()

    def __init__(self):
        self.calls = []

    async def library_status(self):
        self.calls.append("library")
        return {"enabled": True, "commit": "public-commit", "files": ["Definition.lean"],
                "wiki": ["wiki/overview.md"]}

    async def list_notes(self, kind=None):
        self.calls.append(("notes", kind))
        return [{"id": "private-note", "kind": "research.note", "body": "Private research"}]

    async def list_math_artifacts(self):
        self.calls.append("mathematical-artifacts")
        return [{"id": "pending-statement", "kind": "lean.statement"},
                {"id": "pending-proof", "kind": "lean.proof"}]

    async def list_artifacts(self, kind=None):
        self.calls.append(("records", kind))
        return [{"id": "historical-software", "kind": "software.result"}]

    async def native_record(self, record_id, actor):
        assert actor == Actor(role=Role.OPERATOR)
        self.calls.append(("record", record_id))
        return {"id": record_id, "body": "Operator-only record"}

    async def peer_conversations(self, actor, **kwargs):
        assert actor == Actor(role=Role.OPERATOR)
        self.calls.append(("conversations", kwargs))
        return {"worker_id": kwargs["worker_id"], "conversations": [{"id": "conversation"}]}

    async def peer_history(self, conversation_id, actor, **kwargs):
        assert actor == Actor(role=Role.OPERATOR)
        self.calls.append(("history", conversation_id, kwargs))
        return {"conversation_id": conversation_id, "messages": [{"text": "Private message"}]}

    async def peer_unread(self, actor, **kwargs):
        assert actor == Actor(role=Role.OPERATOR)
        self.calls.append(("inbox", kwargs))
        return {"worker_id": kwargs["worker_id"], "delivery_id": None,
                "messages": [{"text": "Private message"}]}


def forbidden_runner(*args, **kwargs):
    raise AssertionError("Inspection must not start a model or run driver")


@pytest.fixture
def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    return tmp_path


def test_all_new_operator_routes_refuse_non_operators_and_create_no_driver(project, monkeypatch):
    hub = ReadHub()
    paths = ("/v1/library", "/v1/notes", "/v1/notes/private-note",
             "/v1/mathematical-artifacts", "/v1/records", "/v1/records/historical-software",
             "/v1/talk/conversations?worker_id=alice",
             "/v1/talk/conversations/conversation?worker_id=alice",
             "/v1/talk/inbox?worker_id=alice")
    with TestClient(create_app(project, hub=hub, runner_factory=forbidden_runner)) as client:
        monkeypatch.setattr("formal_geyser.coordination.api.operator_request", lambda request, token: False)
        for path in paths:
            assert client.get(path).status_code == 401
        assert hub.calls == []
        monkeypatch.setattr("formal_geyser.coordination.api.operator_request", lambda request, token: True)
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, response.text
        assert client.app.state.driver.passes == {}
        assert client.app.state.driver.armed == set()
        assert client.app.state.git_sync_task is None
    assert not (project / ".task-based/state.sqlite3").exists()


def test_math_record_compatibility_and_talk_pagination_route_to_exact_methods(project):
    hub = ReadHub()
    with TestClient(create_app(project, hub=hub, runner_factory=forbidden_runner)) as client:
        assert client.get("/v1/mathematical-artifacts?kind=lean.proof").json() == [
            {"id": "pending-proof", "kind": "lean.proof"}]
        assert client.get("/v1/records").json() == [
            {"id": "historical-software", "kind": "software.result"}]
        assert client.get("/v1/artifacts").json() == [
            {"id": "historical-software", "kind": "software.result"}]
        response = client.get("/v1/talk/conversations/conversation", params={
            "worker_id": "alice", "after_sequence": 4, "offset": 9, "limit": 2, "max_bytes": 512})
        assert response.status_code == 200
        assert hub.calls[-1] == ("history", "conversation", {
            "worker_id": "alice", "after_sequence": 4, "offset": 9, "limit": 2, "max_bytes": 512})
        assert client.get("/v1/talk/inbox?worker_id=alice").json()["delivery_id"] is None
        for query in ("", "?worker_id=", "?worker_id=alice&limit=0", "?worker_id=alice&max_bytes=511"):
            assert client.get("/v1/talk/inbox" + query).status_code == 422


def test_operator_api_cannot_impersonate_a_peer_or_ack_private_delivery(project):
    # Delivery wake eligibility is separate from the authenticated sender's authority.
    with TestClient(create_app(project, runner_factory=forbidden_runner)) as client:
        for name, payload in (
            ("talk.send", {"recipient_id": "bob", "message_id": "forged", "text": "Forged",
                           "sender_id": "alice", "actor": {"role": "worker"}}),
            ("talk.ack", {"delivery_id": "forged"}),
        ):
            result = client.post("/v1/commands", json={
                "name": name, "key": name, "payload": payload})
            assert result.status_code == 403, result.text
        assert client.app.state.driver.passes == {}
        assert client.post("/v1/talk/send", json={}).status_code == 404


@pytest.mark.parametrize("arguments,expected", [
    (["library", "show"], {"enabled": True, "commit": "public-commit",
                             "files": ["Definition.lean"], "wiki": ["wiki/overview.md"]}),
    (["wiki", "show"], {"enabled": True, "commit": "public-commit", "wiki": ["wiki/overview.md"]}),
    (["notes", "list"], [{"id": "private-note", "kind": "research.note", "body": "Private research"}]),
    (["notes", "show", "private-note"], {"id": "private-note", "body": "Operator-only record"}),
    (["artifacts", "list", "--kind", "lean.proof"], [{"id": "pending-proof", "kind": "lean.proof"}]),
    (["records", "list"], [{"id": "historical-software", "kind": "software.result"}]),
    (["records", "show", "historical-software"], {"id": "historical-software", "body": "Operator-only record"}),
    (["talk", "list", "--worker-id", "alice"], {"worker_id": "alice", "conversations": [{"id": "conversation"}]}),
    (["talk", "history", "conversation", "--worker-id", "alice"],
     {"conversation_id": "conversation", "messages": [{"text": "Private message"}]}),
    (["talk", "inbox", "--worker-id", "alice"],
     {"worker_id": "alice", "delivery_id": None, "messages": [{"text": "Private message"}]}),
])
def test_local_operator_cli_uses_native_reads_with_explicit_operator(project, monkeypatch, arguments, expected):
    hub = ReadHub()

    async def local(settings, function):
        return await function(hub)
    monkeypatch.setattr("formal_geyser.coordination.cli._local", local)
    monkeypatch.setattr("formal_geyser.coordination.cli._drive", forbidden_runner)
    result = CliRunner().invoke(app, ["--project", str(project), *arguments])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == expected
    assert hub.calls


@pytest.mark.parametrize("arguments,path", [
    (["library", "show"], "/v1/library"),
    (["notes", "list"], "/v1/notes"),
    (["notes", "show", "record"], "/v1/notes/record"),
    (["artifacts", "list"], "/v1/mathematical-artifacts"),
    (["records", "list"], "/v1/records"),
    (["records", "show", "record"], "/v1/records/record"),
    (["talk", "list", "--worker-id", "alice"], "/v1/talk/conversations"),
    (["talk", "history", "conversation", "--worker-id", "alice"], "/v1/talk/conversations/conversation"),
    (["talk", "inbox", "--worker-id", "alice"], "/v1/talk/inbox"),
])
def test_remote_operator_cli_uses_only_read_endpoints(project, monkeypatch, arguments, path):
    calls = []

    def request(settings, method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return [] if endpoint == "/v1/mathematical-artifacts" else {"ok": True}
    monkeypatch.setattr("formal_geyser.coordination.cli._request", request)
    monkeypatch.setattr("formal_geyser.coordination.cli._local", forbidden_runner)
    result = CliRunner().invoke(app, ["--project", str(project), "--url", "http://127.0.0.1:8765", *arguments])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0][:2] == ("GET", path)
    assert "actor" not in calls[0][2]["params"]
    if arguments[0] == "talk":
        assert calls[0][2]["params"]["worker_id"] == "alice"


def test_cli_talk_requires_worker_selection_and_has_no_send_or_ack_surface(project):
    runner = CliRunner()
    prefix = ["--project", str(project), "talk"]
    for arguments in (["inbox"], ["list"], ["history", "conversation"], ["send"], ["ack"]):
        result = runner.invoke(app, [*prefix, *arguments])
        assert result.exit_code == 2


def test_actual_local_library_and_empty_record_reads_do_not_start_models(project, monkeypatch):
    monkeypatch.setattr("formal_geyser.coordination.cli._drive", forbidden_runner)
    runner = CliRunner()
    prefix = ["--project", str(project)]
    for arguments in (["library", "show"], ["wiki", "show"], ["notes", "list"],
                      ["artifacts", "list"], ["records", "list"]):
        result = runner.invoke(app, [*prefix, *arguments])
        assert result.exit_code == 0, result.output
    result = runner.invoke(app, [*prefix, "run", "list"])
    assert result.exit_code == 0 and json.loads(result.stdout) == []


def test_backup_restores_native_notes_and_rejects_recursive_content_destinations(project, monkeypatch):
    monkeypatch.setattr("formal_geyser.coordination.cli._drive", forbidden_runner)

    async def seed():
        hub = Hub(project, config=ProjectConfig(repository=RepositoryConfig(enabled=True)))
        try:
            await hub.execute(Command(name="repository.initialize", key="native-note-repository"))
            return await stage_note(hub, data={"summary": "Exact native research λ"})
        finally:
            await hub.close()
    note = asyncio.run(seed())
    runner = CliRunner()
    prefix = ["--project", str(project)]
    recursive = project / "notes" / "unsafe-backup"
    rejected = runner.invoke(app, [*prefix, "backup", str(recursive)])
    assert rejected.exit_code == 1 and "native content" in rejected.output
    assert not recursive.exists()
    for root in ("graphs", "drafts"):
        recursive = project / root / "unsafe-backup"
        rejected = runner.invoke(app, [*prefix, "backup", str(recursive)])
        assert rejected.exit_code == 1 and "native content" in rejected.output
        assert not recursive.exists()
    private = project / ".task-based/private/peer-talk/test-fixture"
    private.mkdir(parents=True)
    (private / "message.json").write_text('{"text":"private backup fixture"}', encoding="utf-8")
    backup = project / "backups" / "native-copy"
    copied = runner.invoke(app, [*prefix, "backup", str(backup)])
    assert copied.exit_code == 0, copied.output
    assert (backup / "notes" / note["id"] / "record.json").is_file()
    assert (backup / "graphs/current.json").is_file()
    assert (backup / ".task-based/private/peer-talk/test-fixture/message.json").read_bytes() == (
        private / "message.json").read_bytes()
    restored = runner.invoke(app, ["--project", str(backup), "notes", "show", note["id"]])
    assert restored.exit_code == 0, restored.output
    assert json.loads(restored.stdout)["data"]["summary"] == "Exact native research λ"
    graphs = json.loads((backup / "graphs/current.json").read_text(encoding="utf-8"))
    assert Path(graphs["definitions"]).is_relative_to(backup)
    assert json.loads(Path(graphs["definitions"]).read_bytes()) == {"nodes": [], "edges": []}
