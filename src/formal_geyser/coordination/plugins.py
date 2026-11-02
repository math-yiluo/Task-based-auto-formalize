"""The small, domain-independent contract for validation plugins.

Only the selected project domain is imported. A validator reports evidence;
the Hub alone decides whether the report can become an acceptance receipt.
"""
from __future__ import annotations

from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from jsonschema import exceptions, validators

from .models import TaskContract, ValidationReport

if TYPE_CHECKING:
    from .hub import Hub


@runtime_checkable
class DomainPlugin(Protocol):
    name: str

    def validate_contract(self, contract: TaskContract) -> None:
        """Reject contracts this domain cannot safely evaluate."""

    async def validate_candidate(
        self, hub: Hub, task: dict[str, Any], artifact: dict[str, Any]
    ) -> ValidationReport:
        """Evaluate a candidate without granting it acceptance authority."""


class PluginError(ValueError):
    """The configured plugin or its contract is unavailable or invalid."""


def _check_local_references(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                if not isinstance(item, str) or not item.startswith("#"):
                    raise PluginError("Output schemas may only use local fragment references")
            _check_local_references(item)
    elif isinstance(value, list):
        for item in value:
            _check_local_references(item)


def _schema(contract: TaskContract):
    schema = contract.payload.get("output_schema")
    if not isinstance(schema, (dict, bool)):
        raise PluginError("general.json@1 requires payload.output_schema")
    _check_local_references(schema)
    try:
        validator = validators.validator_for(schema)
        validator.check_schema(schema)  # type: ignore[arg-type]  # Boolean schemas are valid.
    except (exceptions.SchemaError, TypeError, ValueError) as error:
        raise PluginError(f"Invalid output schema: {error}") from error
    return validator(schema)


class GeneralPlugin:
    name = "general"
    policies = frozenset({"general.json@1", "general.peer@1"})

    def validate_publication(self, contract: TaskContract, kind: str, data: dict, policy: str) -> None:
        self.validate_contract(contract)
        if policy == contract.acceptance_policy:
            return
        if (policy != "general.peer@1" or kind != "note.wiki" or not isinstance(data, dict)
                or not isinstance(data.get("summary"), str) or not data["summary"].strip()
                or not isinstance(data.get("paths"), list) or not data["paths"]
                or not all(isinstance(path, str) and path.startswith(("wiki/", "notes/")) and path.endswith(".md")
                           for path in data["paths"])):
            raise PluginError("Only a partial public knowledge note may use a separate peer publication policy")

    def validate_contract(self, contract: TaskContract) -> None:
        if contract.domain != self.name:
            raise PluginError("The general plugin only handles the general domain")
        if contract.acceptance_policy == "general.manual@1":
            raise PluginError(
                "general.manual@1 is retired for task execution; use independent peer review "
                "or JSON validation with independent peer review. Historical receipts remain readable."
            )
        if contract.acceptance_policy not in self.policies:
            raise PluginError(f"Unsupported general policy: {contract.acceptance_policy}")
        if contract.acceptance_policy == "general.json@1":
            _schema(contract)

    async def validate_candidate(
        self, hub: Hub, task: dict[str, Any], artifact: dict[str, Any]
    ) -> ValidationReport:
        contract = TaskContract.model_validate(task["contract"])
        self.validate_contract(contract)
        policy = contract.acceptance_policy
        if policy == "general.peer@1":
            return ValidationReport(
                verdict="accepted", policy=policy,
                details={"scope": "Artifact object structure only; independent peer approval is separately required"},
            )
        validator = _schema(contract)
        try:
            # Limit the report, even when the candidate contains many errors.
            errors = []
            for error in validator.iter_errors(artifact.get("data")):
                errors.append(
                    {
                        "path": list(error.absolute_path),
                        "schema_path": list(error.absolute_schema_path),
                        "message": error.message[:2000],
                    }
                )
                if len(errors) == 20:
                    break
        except Exception as error:
            return ValidationReport(
                verdict="unavailable",
                policy=policy,
                details={"reason": f"Schema evaluation failed: {type(error).__name__}"},
            )
        return ValidationReport(
            verdict="rejected" if errors else "accepted",
            policy=policy,
            details={"errors": errors, "scope": "JSON schema validation only; no semantic review"},
            qualifications=[],
        )


def load_plugin(domain: str) -> DomainPlugin:
    """Load bundled domains first; optional extensions use this application's namespace."""
    if domain == "general":
        return GeneralPlugin()
    if domain == "lean":
        from formal_geyser.plugin import LeanPlugin
        return LeanPlugin()
    matches = list(metadata.entry_points(group="formal_geyser.domain_plugins", name=domain))
    if not matches:
        raise PluginError(f"No installed plugin for domain {domain!r}")
    if len(matches) != 1:
        raise PluginError(f"Multiple installed plugins claim domain {domain!r}")
    try:
        plugin = matches[0].load()()
    except Exception as error:
        raise PluginError(f"Could not load domain {domain!r}: {error}") from error
    if not isinstance(plugin, DomainPlugin) or plugin.name != domain:
        raise PluginError(f"Plugin for {domain!r} does not implement the domain protocol")
    return plugin
