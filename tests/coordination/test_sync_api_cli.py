"""Operator entry points keep optional file sync separate from model execution."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from typer.testing import CliRunner

from formal_geyser.coordination.api import ProjectOwner, create_app
from formal_geyser.coordination.cli import app
from formal_geyser.coordination.config import initialize, load_config
from formal_geyser.coordination.git_sync import GitSyncService
from formal_geyser.coordination.models import (
    GitSyncConfig,
    ProjectConfig,
    RepositoryConfig,
)

runner = CliRunner()


def test_initial_sync_flag_configures_only_and_never_contacts_remote(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Configuration must not contact a remote")

    monkeypatch.setattr("formal_geyser.coordination.git_transport.GitSyncTransport.initialize", forbidden)
    result = runner.invoke(app, ["init", str(tmp_path), "--domain", "general", "--github-sync", "example/library"])
    assert result.exit_code == 0, result.output
    config = load_config(tmp_path)
    assert config.sync.enabled and config.repository.enabled
    assert config.sync.remote == "https://github.com/example/library.git"
    assert "state_branch" not in config.sync.model_dump()
    assert "state_branch" not in (tmp_path / "project.toml").read_text("utf-8")
    assert "contributors" not in config.sync.model_dump()
    assert "contributors" not in (tmp_path / "project.toml").read_text("utf-8")
    assert "max_active" not in config.limits.model_dump()
    assert not (tmp_path / ".task-based/state.sqlite3").exists()
    assert not (tmp_path / ".task-based/repository.git").exists()


def test_configure_and_disable_preserve_project_identity_and_credentials(tmp_path):
    initialize(tmp_path, ProjectConfig(name="Keep the project", domain="lean"))
    before = load_config(tmp_path)
    result = runner.invoke(app, ["--project", str(tmp_path), "sync", "configure", "example/library",
                                 "--poll-seconds", "20"])
    assert result.exit_code == 0, result.output
    configured = load_config(tmp_path)
    assert configured.name == before.name and configured.codex == before.codex
    assert configured.limits == before.limits and configured.sync.poll_seconds == 20
    assert "contributors" not in configured.sync.model_dump()
    assert configured.repository.enabled
    disabled = runner.invoke(app, ["--project", str(tmp_path), "sync", "disable"])
    assert disabled.exit_code == 0, disabled.output
    assert not load_config(tmp_path).sync.enabled
    assert load_config(tmp_path).sync.remote == configured.sync.remote


def test_sync_configuration_has_no_contributor_or_separate_state_branch_option():
    result = runner.invoke(app, ["sync", "configure", "--help"])
    assert result.exit_code == 0, result.output
    assert "state-branch" not in result.output
    assert "contributor" not in result.output


def test_configuration_cannot_replace_owned_project_settings(tmp_path):
    initialize(tmp_path, ProjectConfig())
    before = (tmp_path / "project.toml").read_bytes()
    with ProjectOwner(tmp_path):
        result = runner.invoke(app, ["--project", str(tmp_path), "sync", "configure", "example/library"])
    assert result.exit_code == 1
    assert (tmp_path / "project.toml").read_bytes() == before


def test_disabled_sync_command_is_a_noop_and_external_commands_are_absent(tmp_path):
    initialize(tmp_path, ProjectConfig())
    prefix = ["--project", str(tmp_path), "sync"]
    result = runner.invoke(app, [*prefix, "once"])
    assert result.exit_code == 0 and json.loads(result.output)["state"] == "disabled"
    for command in ("records", "contributions"):
        result = runner.invoke(app, [*prefix, command])
        assert result.exit_code == 2
        assert "No such command" in result.output
    assert not (tmp_path / ".task-based/sync-cache").exists()


async def test_sync_api_requires_operator_and_external_routes_are_absent(tmp_path):
    initialize(tmp_path, ProjectConfig())

    def forbidden_runner(hub):
        raise AssertionError("File synchronization cannot start a model driver")

    api = create_app(tmp_path, runner_factory=forbidden_runner)
    async with api.router.lifespan_context(api):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver") as client:
            response = await client.get("/v1/sync")
            assert response.status_code == 200 and "contributors" not in response.json()
            for path in ("/v1/external/records", "/v1/external/contributions"):
                assert path not in api.openapi()["paths"]
                assert (await client.get(path)).status_code == 404
                assert (await client.get(path)).status_code == 404
            response = await client.post("/v1/sync")
            assert response.json() == {"enabled": False, "state": "disabled"}
            assert api.state.git_sync_task is None
            assert await api.state.hub.list_runs() == []


async def test_enabled_service_starts_and_stops_only_its_sync_loop(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig(repository=RepositoryConfig(enabled=True),
        sync=GitSyncConfig(enabled=True, remote="example/library")))
    started, stopped = asyncio.Event(), asyncio.Event()

    async def scripted(self):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(GitSyncService, "run", scripted)

    def forbidden_runner(hub):
        raise AssertionError("Enabling sync cannot start models")

    api = create_app(tmp_path, runner_factory=forbidden_runner)
    async with api.router.lifespan_context(api):
        await asyncio.wait_for(started.wait(), 2)
        assert await api.state.hub.list_runs() == []
        assert not api.state.driver.armed
    assert stopped.is_set()


@pytest.mark.parametrize("armed", [False, True])
async def test_sync_does_not_wake_any_model_driver(tmp_path, monkeypatch, armed):
    initialize(tmp_path, ProjectConfig())
    drives = []

    class Runner:
        def __init__(self, hub):
            pass

        async def run_until_idle(self, run_id, **_):
            drives.append(run_id)
            return {"steps": 0}

    async def scripted_once(self):
        return {"state": "synced"}

    monkeypatch.setattr(GitSyncService, "once", scripted_once)
    api = create_app(tmp_path, runner_factory=Runner)
    async with api.router.lifespan_context(api):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://testserver") as client:
            response = await client.post("/v1/commands",
                json={"name": "run.start", "key": "start", "payload": {}})
            run_id = response.json()["id"]
            if armed:
                api.state.driver.armed.add(run_id)
            assert (await client.post("/v1/sync")).status_code == 200
            pending = list(api.state.driver.passes.values())
            if pending:
                await asyncio.gather(*pending)
            assert drives == []



def test_removed_contributor_option_is_rejected_without_changing_config(tmp_path):
    initialize(tmp_path, ProjectConfig())
    config_path = tmp_path / "project.toml"
    before = config_path.read_bytes()
    result = runner.invoke(app, ["--project", str(tmp_path), "sync", "configure", "example/library",
                                "--contributor", "legacy=contrib/legacy"])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("enabled", [False, True])
def test_removed_contributor_config_is_rejected_without_rewriting_it(tmp_path, enabled):
    initialize(tmp_path, ProjectConfig(repository=RepositoryConfig(enabled=True),
        sync=GitSyncConfig(enabled=enabled, remote="example/library")))
    path = tmp_path / "project.toml"
    path.write_text(path.read_text("utf-8") + '\n[[sync.contributors]]\nid = "legacy"\nbranch = "contrib/legacy"\n',
                    encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_config(tmp_path)
    assert path.read_bytes() == before
