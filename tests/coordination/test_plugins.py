from types import SimpleNamespace

import pytest

from formal_geyser.coordination.models import TaskContract
from formal_geyser.coordination.plugins import GeneralPlugin, PluginError, load_plugin


@pytest.mark.asyncio
async def test_default_policy_retains_independent_peer_review():
    contract = TaskContract(title="Review a document", objective="Check the document")
    result = await GeneralPlugin().validate_candidate(
        None, {"contract": contract.model_dump()}, {"data": {"ready": True}}
    )
    assert result.verdict == "accepted"
    assert result.policy == "general.peer@1"
    assert "independent peer approval is separately required" in result.details["scope"]


@pytest.mark.asyncio
async def test_retired_manual_policy_cannot_create_a_final_approval_dead_end():
    contract = TaskContract(
        title="Historical manual task", objective="Inspect retained evidence",
        acceptance_policy="general.manual@1",
    )
    # The recorded value remains decodable without enabling task execution.
    assert contract.acceptance_policy == "general.manual@1"
    with pytest.raises(PluginError, match="retired for task execution"):
        GeneralPlugin().validate_publication(contract, "software.result", {}, "general.manual@1")
    with pytest.raises(PluginError, match="retired for task execution"):
        await GeneralPlugin().validate_candidate(
            None, {"contract": contract.model_dump()}, {"data": {"ready": True}}
        )


@pytest.mark.asyncio
async def test_json_validation_uses_contract_schema_and_ignores_candidate_policy():
    contract = TaskContract(
        title="Produce a status record",
        objective="Produce a structured status",
        acceptance_policy="general.json@1",
        payload={
            "output_schema": {
                "type": "object",
                "properties": {"ready": {"type": "boolean"}},
                "required": ["ready"],
                "additionalProperties": False,
            }
        },
    )
    plugin = GeneralPlugin()
    valid = await plugin.validate_candidate(
        None, {"contract": contract.model_dump()},
        {"data": {"ready": True}, "acceptance_policy": "worker.claimed@1"},
    )
    invalid = await plugin.validate_candidate(
        None, {"contract": contract.model_dump()}, {"data": {"ready": "yes"}}
    )
    assert valid.verdict == "accepted"
    assert valid.policy == "general.json@1"
    assert valid.qualifications == []
    assert valid.details["scope"].startswith("JSON schema validation only")
    assert invalid.verdict == "rejected"
    assert invalid.details["errors"][0]["path"] == ["ready"]


@pytest.mark.parametrize("schema", [
    {"$ref": "https://example.invalid/schema"},
    {"$defs": {"nested": {"$dynamicRef": "file:///private"}}},
    {"type": "not-a-json-type"},
    None,
])
def test_json_policy_rejects_remote_references_and_invalid_schemas(schema):
    contract = TaskContract(
        title="Check a schema", objective="Validate a record",
        acceptance_policy="general.json@1", payload={"output_schema": schema},
    )
    with pytest.raises(PluginError):
        GeneralPlugin().validate_contract(contract)


@pytest.mark.parametrize("domain,policy", [
    ("uninstalled", "general.manual@1"),
    ("general", "shell.run@1"),
])
def test_general_plugin_cannot_cross_domain_or_run_custom_evaluators(domain, policy):
    contract = TaskContract(
        title="Check policy", objective="Inspect policy",
        domain=domain, acceptance_policy=policy,
        payload={"command": ["some-executable", "run"]},
    )
    with pytest.raises(PluginError):
        GeneralPlugin().validate_contract(contract)


def test_default_core_plugin_does_not_import_domain_packages(monkeypatch):
    def discovery(**kwargs):
        pytest.fail("General core operation must not discover child application plugins")

    monkeypatch.setattr("formal_geyser.coordination.plugins.metadata.entry_points", discovery)
    assert isinstance(load_plugin("general"), GeneralPlugin)


def test_only_enabled_plugin_is_loaded(monkeypatch):
    class SelectedPlugin(GeneralPlugin):
        name = "selected"

    def discovery(**kwargs):
        assert kwargs == {"group": "formal_geyser.domain_plugins", "name": "selected"}
        return [SimpleNamespace(load=lambda: SelectedPlugin)]

    monkeypatch.setattr("formal_geyser.coordination.plugins.metadata.entry_points", discovery)
    assert load_plugin("selected").name == "selected"


def test_missing_and_duplicate_plugins_fail_explicitly(monkeypatch):
    monkeypatch.setattr("formal_geyser.coordination.plugins.metadata.entry_points", lambda **kwargs: [])
    with pytest.raises(PluginError, match="No installed"):
        load_plugin("missing")
    monkeypatch.setattr(
        "formal_geyser.coordination.plugins.metadata.entry_points",
        lambda **kwargs: [SimpleNamespace(), SimpleNamespace()],
    )
    with pytest.raises(PluginError, match="Multiple"):
        load_plugin("duplicate")


def test_incorrect_plugin_protocol_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "formal_geyser.coordination.plugins.metadata.entry_points",
        lambda **kwargs: [SimpleNamespace(load=lambda: lambda: object())],
    )
    with pytest.raises(PluginError, match="protocol"):
        load_plugin("broken")
