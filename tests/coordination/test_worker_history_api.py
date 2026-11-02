"""Worker history pages inspect exact records without arming model execution."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Command, ProjectConfig
from formal_geyser.coordination.transcripts import (
    TranscriptStore,
    inline_message_bodies,
)


def no_models(*args, **kwargs):
    raise AssertionError("History inspection must never start a model")


@pytest.fixture
def history_project(tmp_path):
    initialize(tmp_path, ProjectConfig())

    async def seed():
        hub = Hub(tmp_path)
        try:
            for worker_id in ("original-internal-id", "unrelated-internal-id"):
                await hub.execute(Command(name="worker.register", key=worker_id,
                                          payload={"worker_id": worker_id}))
        finally:
            await hub.close()

    asyncio.run(seed())
    store = TranscriptStore(tmp_path)
    store.record_instructions("original-internal-id", "native-session", {"text": "Read the wiki."})
    for number in range(25):
        input_id = store.prepare_input("original-internal-id", "native-session", "taskhub",
                                       f"Exact prompt {number}\nSecond line", kind="continuation")
        store.delivered(input_id, f"turn-{number}")
        store.capture_native("native-session", "original-internal-id", "item/completed", {
            "turnId": f"turn-{number}", "item": {"type": "agentMessage", "id": f"reply-{number}",
                                                   "phase": "finalAnswer", "text": f"Exact response {number}"}})
        store.capture_native("native-session", "original-internal-id", "turn/completed", {
            "turn": {"id": f"turn-{number}", "status": "completed"}})
    store.flush()  # The recorded history is on disk before the service reads it.
    return tmp_path


def test_history_assets_and_authentication(history_project):
    root = history_project
    with TestClient(create_app(root, runner_factory=no_models)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'aria-label="Selected person' in page.text
        assert "Reviewers" in page.text and "Workers" in page.text
        assert 'src="/dashboard.js"' in page.text
        nonce = re.search(r'nonce="([^"]+)"', page.text).group(1)
        assert "'nonce-" + nonce + "'" in page.headers["Content-Security-Policy"]
        script = client.get("/dashboard.js")
        assert script.status_code == 200 and "WorkerHistoryStore" in script.text
        assert script.headers["Content-Type"].startswith("text/javascript")
        assert client.app.state.driver.passes == {} and client.app.state.driver.armed == set()


def test_sessions_page_latest_then_older_with_exact_text_and_worker_isolation(history_project):
    root = history_project
    transcript = next((root / ".task-based/transcripts/workers").glob("*/events.jsonl"))
    original = transcript.read_bytes()
    with TestClient(create_app(root, runner_factory=no_models)) as client:
        overview = client.get("/v1/overview").json()
        assert [w["display_name"] for w in overview["workers"]] == ["Worker 0", "Worker 1"]
        sessions = client.get("/v1/workers/Worker%200/sessions").json()["sessions"]
        assert len(sessions) == 1 and sessions[0]["turn_count"] == 25
        endpoint = "/v1/workers/Worker%200/sessions/" + sessions[0]["key"]
        latest = client.get(endpoint).json()
        assert len(latest["turns"]) == 20 and latest["has_more"]
        assert latest["turns"][-1]["inputs"][0]["text"] == "Exact prompt 24\nSecond line"
        assert latest["turns"][-1]["replies"][0]["text"] == "Exact response 24"
        older = client.get(endpoint, params={"before": latest["next_before"], "include_context": False}).json()
        assert len(older["turns"]) == 5 and not older["has_more"] and older["context"] == []
        assert len({t["key"] for t in older["turns"] + latest["turns"]}) == 25
        assert client.get("/v1/workers/Worker%201/sessions").json() == {"sessions": []}
        assert client.get("/v1/workers/Worker%201/sessions/" + sessions[0]["key"]).status_code == 404
        for query in ("?before=0", "?limit=0", "?limit=101", "?include_context=nonsense"):
            assert client.get(endpoint + query).status_code == 422
        assert client.app.state.driver.passes == {} and client.app.state.driver.armed == set()
    assert transcript.read_bytes() == original


def test_session_page_inlines_only_correspondence_and_peer_talk_bodies(history_project):
    root = history_project
    letter = root / "correspondence" / "to-worker.md"
    letter.parent.mkdir(parents=True, exist_ok=True)
    letter.write_bytes(b'```json\r\n{"sender": "user"}\r\n```\r\n\r\nCheck duality first.\n')
    talk = root / ".task-based" / "private" / "peer-talk" / "note.json"
    talk.parent.mkdir(parents=True, exist_ok=True)
    talk.write_text(json.dumps({"text": "I take smoothness."}), encoding="utf-8")
    secret = root / ".task-based" / "state.sqlite3"
    references = [{"sender": "user", "sender_role": "user", "file": {"path": str(letter)}},
                  {"sender": "Worker 1", "sender_role": "worker", "file": {"path": ".task-based/private/peer-talk/note.json"}},
                  {"sender": "user", "file": {"path": str(secret)}},
                  {"sender": "user", "file": {"path": "correspondence/../.task-based/state.sqlite3"}},
                  {"sender": "user", "file": {"path": str(root / "correspondence" / "missing.md")}}]
    store = TranscriptStore(root)
    packet = {"human_messages": {"messages": references[:1] + references[2:]},
              "private_talk": {"messages": references[1:2]}}
    input_id = store.prepare_input("original-internal-id", "native-session", "taskhub",
                                   json.dumps(packet), kind="steer")
    store.delivered(input_id, "turn-24")
    with TestClient(create_app(root, runner_factory=no_models)) as client:
        page = client.get("/v1/workers/Worker%200/sessions/thread:native-session").json()
    record = next(item for turn in page["turns"] for item in turn["inputs"] if item.get("messages"))
    assert [(m["sender"], m["text"]) for m in record["messages"]] == [
        ("user", "Check duality first.\n"), ("user", None), ("user", None), ("user", None),
        ("Worker 1", "I take smoothness.")]
    assert json.loads(record["text"]) == packet  # The exact model input is unchanged.
    assert inline_message_bodies({"turns": [{"inputs": [{"text": "plain prompt"}]}]}, root) == {
        "turns": [{"inputs": [{"text": "plain prompt"}]}]}


def test_frontend_state_races_without_browser_or_models():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is an optional development check, never a runtime dependency")
    script = Path(__file__).with_name("dashboard_state.test.cjs")
    result = subprocess.run([executable, "--test", str(script)], capture_output=True,
                            text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
