"""Withdrawal CLI keeps the required reason through local and service routing."""
import asyncio
import json

import pytest
from typer.testing import CliRunner

from formal_geyser.coordination.cli import app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role

runner = CliRunner()


def test_withdraw_records_reason_and_preserves_dependency_history(tmp_path, monkeypatch):
    initialize(tmp_path, ProjectConfig())

    async def prepare():
        hub = Hub(tmp_path)
        try:
            original = await hub.execute(Command(name="artifact.register", key="original", payload={
                "kind": "general.record", "data": {"text": "Keep the original record"},
            }), Actor(role=Role.OPERATOR))
            dependent = await hub.execute(Command(name="artifact.register", key="dependent", payload={
                "kind": "general.record", "data": {"text": "Keep the dependent record"},
                "input_refs": [original["id"]],
            }), Actor(role=Role.OPERATOR))
            return original, dependent
        finally:
            await hub.close()

    original, dependent = asyncio.run(prepare())

    def forbidden_driver(*args, **kwargs):
        raise AssertionError("Withdrawal must not start model execution")

    monkeypatch.setattr("formal_geyser.coordination.cli._drive", forbidden_driver)
    reason = "The source assumption was disproved; retain both records for inspection."
    result = runner.invoke(app, ["--project", str(tmp_path), "artifacts", "withdraw", original["id"],
                                 "--reason", reason])
    assert result.exit_code == 0, result.output
    assert set(json.loads(result.stdout)["withdrawn"]) == {original["id"], dependent["id"]}

    async def inspect():
        hub = Hub(tmp_path)
        try:
            for previous in (original, dependent):
                record = await hub.artifact(previous["id"])
                assert record["status"] == "withdrawn"
                assert record["data"] == previous["data"]
            events = [event for event in await hub.events() if event["kind"] == "artifact.withdrawn"]
            assert {event["object_id"] for event in events} == {original["id"], dependent["id"]}
            assert all(event["payload"]["reason"] == reason for event in events)
        finally:
            await hub.close()

    asyncio.run(inspect())


def test_withdraw_service_route_passes_reason_without_driving(monkeypatch, tmp_path):
    calls = []

    def request(settings, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"withdrawn": ["candidate-1"]}

    monkeypatch.setattr("formal_geyser.coordination.cli._request", request)
    reason = "This contribution is no longer required."
    result = runner.invoke(app, ["--project", str(tmp_path), "--url", "http://127.0.0.1:8765",
                                 "artifacts", "withdraw", "candidate-1", "--reason", reason])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    method, path, kwargs = calls[0]
    assert (method, path) == ("POST", "/v1/commands")
    assert kwargs["json"]["name"] == "artifact.withdraw"
    assert kwargs["json"]["payload"] == {"artifact_id": "candidate-1", "reason": reason}


@pytest.mark.parametrize("options", [[], ["--reason", ""], ["--reason", " \t "]])
def test_withdraw_rejects_missing_or_blank_reason_before_dispatch(monkeypatch, options):
    calls = []

    def command(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Invalid withdrawal must not reach the project")

    monkeypatch.setattr("formal_geyser.coordination.cli._command", command)
    result = runner.invoke(app, ["artifacts", "withdraw", "candidate-1", *options])
    assert result.exit_code == 2, result.output
    assert "reason" in result.output
    assert calls == []
