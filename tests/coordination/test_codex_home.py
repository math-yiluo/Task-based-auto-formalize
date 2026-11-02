from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from formal_geyser.coordination.cli import app
from formal_geyser.coordination.config import (
    codex_profiles_equal,
    initialize,
    load_config,
)
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    CodexConfig,
    Command,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime.codex import CodexHarness


async def execute(hub, name, payload, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload), Actor(role=role))


def test_explicit_home_round_trips_relative_to_project_and_never_mutates_parent(tmp_path, monkeypatch):
    inherited = str(tmp_path / "desktop")
    monkeypatch.setenv("CODEX_HOME", inherited)
    root = tmp_path / "project"
    initialize(root, ProjectConfig(codex=CodexConfig(home="profile")))
    config = load_config(root)
    assert config.codex.home == str((root / "profile").resolve())
    assert not (root / "profile").exists()
    assert os.environ["CODEX_HOME"] == inherited
    monkeypatch.chdir(tmp_path)
    assert load_config(root).codex.home == config.codex.home


def test_general_domain_retains_inherited_home_default(tmp_path):
    initialize(tmp_path, ProjectConfig())
    assert load_config(tmp_path).codex.home is None
    harness = CodexHarness(CodexConfig(), command=[sys.executable])
    assert harness.process_environment() is None


def test_profiles_differ_by_home_and_version_but_not_by_frame_size():
    current = CodexConfig().model_dump(mode="json")
    assert codex_profiles_equal({**current, "max_frame_bytes": 8388608}, current)
    assert not codex_profiles_equal(current, {**current, "home": "/new-profile"})
    assert not codex_profiles_equal({**current, "version": "different"}, current)
    assert not codex_profiles_equal(None, current)


def test_cli_records_explicit_home(tmp_path):
    root = tmp_path / "project"
    home = tmp_path / "profile"
    result = CliRunner().invoke(app, ["init", str(root), "--codex-home", str(home)])
    assert result.exit_code == 0, result.output
    assert load_config(root).codex.home == str(home.resolve())


def test_launch_pins_sqlite_home_above_inherited_config(tmp_path):
    home = tmp_path / "isolated home"
    command = CodexHarness.launch_command(CodexConfig(executable=sys.executable, home=str(home)))
    assert command[-1] == "app-server"
    assert "sqlite_home=" + json.dumps(str(home.resolve())) in command


PEER = r"""
import json,os,sys
from pathlib import Path
log=Path(sys.argv[1])
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if method=='initialize':
        value={'userAgent':'Codex Desktop/0.160.0'}
    elif method in ('thread/start','thread/resume'):
        home=Path(os.environ['CODEX_HOME'])
        with log.open('a',encoding='utf-8') as output:
            output.write(json.dumps({'method':method,'home':str(home),
              'sqlite_home':os.environ['CODEX_SQLITE_HOME'],'cwd':str(Path.cwd()),
              'guidance':(home/'AGENTS.md').read_text(encoding='utf-8'),
              'params':message['params']})+'\n')
        value={'thread':{'id':message['params'].get('threadId','native-thread')},'model':'scripted'}
    elif method=='turn/start':
        raise AssertionError('Metadata verification must not start inference')
    else:
        continue
    print(json.dumps({'id':message['id'],'result':value}),flush=True)
"""


async def test_real_child_uses_same_home_for_start_resume_and_all_workspaces(tmp_path, monkeypatch):
    home = tmp_path / "dedicated profile"
    home.mkdir()
    (home / "AGENTS.md").write_text("English output. Report evidence accurately.", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unrelated-desktop"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "unrelated-database"))
    before = dict(os.environ)
    script = tmp_path / "peer.py"
    script.write_text(PEER, encoding="utf-8")
    log = tmp_path / "seen.jsonl"
    for index, thread in enumerate((None, "native-thread", None)):
        workspace = tmp_path / f"worker-{index}"
        config = CodexConfig(home=str(home), version="0.160.0")
        harness = CodexHarness(config, command=[sys.executable, "-u", str(script), str(log)])
        try:
            await harness.open(workspace=workspace, thread_id=thread, tools=[], instructions="Runtime rules.")
        finally:
            await harness.close()
        assert harness.process_quiet
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["method"] for row in rows] == ["thread/start", "thread/resume", "thread/start"]
    for row in rows:
        assert Path(row["home"]) == home
        assert Path(row["sqlite_home"]) == home
        assert row["params"]["config"]["sqlite_home"] == str(home.resolve())
        assert row["guidance"] == "English output. Report evidence accurately."
        assert row["params"]["developerInstructions"] == "Runtime rules."
        assert row["params"]["approvalPolicy"] == "never"
    assert os.environ == before


@pytest.mark.parametrize("recorded_home", [None, "old-home"])
async def test_bound_session_cannot_be_claimed_under_new_home(tmp_path, recorded_home):
    home = str((tmp_path / "new-home").resolve())
    initialize(tmp_path, ProjectConfig(codex=CodexConfig(home=home)))
    hub = Hub(tmp_path)
    try:
        task = await execute(hub, "task.publish", {"contract": {
            "title": "Profile isolation", "objective": "Inspect a configuration record."}})
        worker = await execute(hub, "worker.register", {})
        await execute(hub, "worker.bind_session", {
            "worker_id": worker["id"], "thread_id": "original-native-thread",
            "codex_profile": CodexConfig(home=recorded_home).model_dump(mode="json")}, Role.RUNNER)
        run = await execute(hub, "run.start", {})
        with pytest.raises(HubError) as error:
            await execute(hub, "task.claim", {"task_id": task["id"], "revision": 1,
                "worker_id": worker["id"], "run_id": run["id"]}, Role.RUNNER)
        assert error.value.code == "PROFILE_HOME_CHANGED"
        assert (await hub.worker(worker["id"]))["thread_id"] == "original-native-thread"
        assert (await hub.task(task["id"]))["active_attempt"] is None
        assert (await hub.run(run["id"]))["turns_used"] == 0
    finally:
        await hub.close()


async def test_bound_session_can_be_reclaimed_in_recorded_home(tmp_path):
    home = str((tmp_path / "profile").resolve())
    initialize(tmp_path, ProjectConfig(codex=CodexConfig(home=home)))
    hub = Hub(tmp_path)
    try:
        task = await execute(hub, "task.publish", {"contract": {
            "title": "Profile continuity", "objective": "Inspect a configuration record."}})
        worker = await execute(hub, "worker.register", {})
        await execute(hub, "worker.bind_session", {
            "worker_id": worker["id"], "thread_id": "original-native-thread", "codex_home": home,
            "codex_profile": hub.config.codex.model_dump(mode="json")}, Role.RUNNER)
        run = await execute(hub, "run.start", {})
        attempt = await execute(hub, "task.claim", {"task_id": task["id"], "revision": 1,
            "worker_id": worker["id"], "run_id": run["id"]}, Role.RUNNER)
        assert attempt["worker_id"] == worker["id"]
        assert (await hub.worker(worker["id"]))["thread_id"] == "original-native-thread"
    finally:
        await hub.close()
