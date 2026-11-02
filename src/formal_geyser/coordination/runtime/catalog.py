"""Compare complete native dynamic catalogs without changing saved conversations."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..errors import HubError
from ..storage import canonical


class CatalogError(ValueError):
    """The saved catalog cannot establish the exact callable tool definitions."""


def _alias(item: dict[str, Any], camel: str, snake: str, *, default: Any = None) -> Any:
    if camel in item and snake in item and canonical(item[camel]) != canonical(item[snake]):
        raise CatalogError(f"Conflicting {camel}/{snake} serialization aliases")
    return item.get(camel, item.get(snake, default))


def normalize_catalog(catalog: Any) -> list[dict[str, Any]]:
    """Normalize only audited wire/storage spelling and optional-field defaults.

    Descriptions and schemas remain exact JSON values. In particular, do not
    remove schema annotations, rewrite required lists, or accept name supersets.
    """
    if not isinstance(catalog, list):
        raise CatalogError("Dynamic tools must be a list of complete definitions")
    normalized = []
    names: set[str] = set()
    fields = {"type", "name", "description", "inputSchema", "input_schema",
              "namespace", "deferLoading", "defer_loading"}
    for item in catalog:
        if not isinstance(item, dict) or any(key not in fields for key in item):
            raise CatalogError("A dynamic tool contains unsupported or malformed fields")
        name = item.get("name")
        if not isinstance(name, str) or not name or name != name.strip():
            raise CatalogError("A dynamic tool has a missing or malformed name")
        if name in names:
            raise CatalogError(f"Duplicate dynamic tool name: {name}")
        names.add(name)
        if item.get("type", "function") != "function":
            raise CatalogError(f"Unsupported dynamic tool type: {name}")
        description = item.get("description")
        schema = _alias(item, "inputSchema", "input_schema")
        namespace = item.get("namespace")
        deferred = _alias(item, "deferLoading", "defer_loading", default=False)
        if not isinstance(description, str) or not isinstance(schema, dict):
            raise CatalogError(f"Missing description or object input schema: {name}")
        if namespace is not None and (not isinstance(namespace, str) or not namespace):
            raise CatalogError(f"Malformed namespace: {name}")
        if type(deferred) is not bool:
            raise CatalogError(f"Malformed deferred-loading flag: {name}")
        try:
            json.dumps(item, allow_nan=False)
            Draft202012Validator.check_schema(schema)
        except (TypeError, ValueError, SchemaError) as exc:
            raise CatalogError(f"Malformed input schema or non-JSON definition: {name}") from exc
        tool: dict[str, Any] = {"type": "function", "name": name, "description": description,
                                "inputSchema": deepcopy(schema)}
        if namespace is not None:
            tool["namespace"] = namespace
        if deferred:
            tool["deferLoading"] = True
        normalized.append(tool)
    return sorted(normalized, key=lambda tool: tool["name"])


def catalog_binding(catalog: Any) -> dict[str, Any]:
    normalized = normalize_catalog(catalog)
    return {"tool_catalog_version": 3,
            "tool_catalog_names": [tool["name"] for tool in normalized],
            "tool_catalog_hash": hashlib.sha256(canonical(normalized).encode()).hexdigest()}


def catalog_difference(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> str:
    wanted, saved = ({tool["name"]: canonical(tool) for tool in catalog}
                     for catalog in (expected, actual))
    differences = []
    for label, names in (
        ("Missing tools", wanted.keys() - saved.keys()),
        ("Obsolete tools", saved.keys() - wanted.keys()),
        ("Changed definitions", {name for name in wanted.keys() & saved.keys()
                                 if wanted[name] != saved[name]}),
    ):
        if names:
            differences.append(label + ": " + ", ".join(sorted(names)))
    return "; ".join(differences)


def migration_required(thread_id: str, reason: str) -> HubError:
    return HubError(
        "NATIVE_CATALOG_MIGRATION_REQUIRED",
        f"The persisted dynamic-tool catalog for thread {thread_id!r} is incompatible or "
        f"unverified. {reason}. The pinned native resume protocol has no audited catalog "
        "replacement operation. The original conversation and workspace remain preserved; "
        "no inference was started. Keep this execution paused until a reviewed migration "
        "can preserve that conversation. A separately initialized worker needs a deliberate "
        "context handoff; it does not silently continue this native thread.",
    )
