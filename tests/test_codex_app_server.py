"""The audited Codex app-server starts with the workers' launch command and opens both kinds of
thread: a worker's, in the worker profile, and a decision helper's, read-only. No turn starts,
so no model is called."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from formal_geyser.coordination.models import CodexConfig
from formal_geyser.coordination.native_permissions import WORKER_PROFILE
from formal_geyser.coordination.runtime.codex import CodexHarness
from formal_geyser.coordination.runtime.executable import native_candidates

CODEX = next((path for path in native_candidates(
    Path(__file__).resolve().parents[1] / ".tools/codex-0.160.0/node_modules/@openai") if path.is_file()), None)


@pytest.mark.skipif(CODEX is None, reason="the audited Codex in .tools")
def test_the_app_server_starts_and_opens_worker_and_helper_threads(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    command = CodexHarness.launch_command(CodexConfig(executable=str(CODEX), home=str(home)))
    server = subprocess.Popen(command, cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(home)})
    assert server.stdin is not None and server.stdout is not None and server.stderr is not None

    def call(number, method, params):
        server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params}) + "\n")
        server.stdin.flush()
        for line in server.stdout:
            message = json.loads(line)
            if message.get("id") == number:
                return message
        raise AssertionError("The app-server exited: " + server.stderr.read()[-2000:])

    try:
        assert "result" in call(1, "initialize", {"clientInfo": {"name": "test", "version": "0"},
                                                  "capabilities": {"experimentalApi": True}})
        server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
        worker = call(2, "thread/start", {"cwd": str(work), "permissions": WORKER_PROFILE})
        helper = call(3, "thread/start", {"cwd": str(work), "sandbox": "read-only"})
    finally:
        server.stdin.close()
        server.terminate()
        server.wait(timeout=30)
    sandbox = worker["result"]["sandbox"]
    assert sandbox["type"] == "workspaceWrite" and sandbox["networkAccess"] and sandbox["excludeSlashTmp"]
    assert helper["result"]["sandbox"] == {"type": "readOnly", "networkAccess": False}
