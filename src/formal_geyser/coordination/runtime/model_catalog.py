"""Discover Codex picker metadata without opening a thread or starting a turn."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any, Sequence

from ..errors import HubError
from ..models import CodexConfig
from ..storage import collected
from .codex import CodexHarness, audited_version, initialize
from .process import OwnedProcessHost, ProcessHostError, ProcessTreeNotQuiet
from .rpc import JsonRpcClient, RpcError, RpcProtocolError, RpcTimeout


class ModelCatalogError(HubError):
    """A catalog failure that is safe to expose to the project operator."""


def _invalid(field: str) -> ModelCatalogError:
    return ModelCatalogError(
        "model_catalog_invalid",
        f"Codex returned invalid model catalog metadata ({field}). "
        "Refresh the catalog and verify the configured audited Codex version.",
        status=502,
    )


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _invalid(field)
    return value


def _model(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        raise _invalid("model/list.data entry")
    if not isinstance(record.get("hidden"), bool):
        raise _invalid("hidden")
    if record["hidden"]:
        return None
    _text(record.get("id"), "id")
    model_id = _text(record.get("model"), "model")
    name = _text(record.get("displayName"), "displayName")
    description = _text(record.get("description", ""), "description", allow_empty=True)
    if not isinstance(record.get("isDefault"), bool):
        raise _invalid("isDefault")
    supported = record.get("supportedReasoningEfforts")
    if not isinstance(supported, list) or not supported:
        raise _invalid("supportedReasoningEfforts")
    efforts: dict[str, dict[str, str]] = {}
    for effort in supported:
        if not isinstance(effort, dict):
            raise _invalid("supportedReasoningEfforts entry")
        effort_id = _text(effort.get("reasoningEffort"), "reasoningEffort")
        normalized = {
            "id": effort_id,
            "description": _text(effort.get("description"), "effort description", allow_empty=True),
        }
        if effort_id in efforts and efforts[effort_id] != normalized:
            raise _invalid("conflicting reasoning effort metadata")
        efforts[effort_id] = normalized
    default_effort = _text(record.get("defaultReasoningEffort"), "defaultReasoningEffort")
    if default_effort not in efforts:
        raise _invalid("defaultReasoningEffort is not supported")
    return {
        "id": model_id,
        "name": name,
        "description": description,
        "efforts": list(efforts.values()),
        "default_effort": default_effort,
        "is_default": record["isDefault"],
    }


def _authentication_required(result: Any) -> bool:
    if (not isinstance(result, dict) or "account" not in result
            or not isinstance(result.get("requiresOpenaiAuth"), bool)):
        raise _invalid("account/read")
    account = result["account"]
    if account is not None:
        if not isinstance(account, dict):
            raise _invalid("account/read.account")
        _text(account.get("type"), "account/read.account.type")
    # Provider requirements alone do not mean that an authenticated user must log in.
    # Deliberately return no account fields, tokens, email address, or plan metadata.
    return result["requiresOpenaiAuth"] and account is None


async def _models(rpc: JsonRpcClient) -> list[dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    cursors: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 20, "includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        page = await rpc.request("model/list", params)
        if (not isinstance(page, dict) or not isinstance(page.get("data"), list)
                or "nextCursor" not in page):
            raise _invalid("model/list page")
        for record in page["data"]:
            model = _model(record)
            if model is None:
                continue
            model_id = model["id"]
            if model_id in models and models[model_id] != model:
                raise _invalid("conflicting model metadata across pages")
            models[model_id] = model
        if page["nextCursor"] is None:
            break
        cursor = _text(page["nextCursor"], "nextCursor")
        if cursor in cursors:
            raise _invalid("repeated pagination cursor")
        cursors.add(cursor)
    if sum(model["is_default"] for model in models.values()) > 1:
        raise _invalid("multiple default models")
    return list(models.values())


async def _close(host: OwnedProcessHost, rpc: JsonRpcClient | None, grace: float) -> None:
    try:
        try:
            await host.stop(grace=grace)
            if not host.tree_quiet:
                raise ProcessTreeNotQuiet("Metadata process tree is still active")
        except (OSError, ProcessHostError):
            raise ModelCatalogError(
                "model_catalog_cleanup_failed",
                "The Codex metadata process could not be confirmed stopped. "
                "Resolve the remaining process before refreshing the model catalog.",
                status=503,
            ) from None
        if rpc is not None:
            await rpc.finish_receiving(timeout=grace)
            await rpc.drain_callbacks(timeout=grace)
    finally:
        if rpc is not None:
            await rpc.close()


async def _finish_close(host: OwnedProcessHost, rpc: JsonRpcClient | None, grace: float) -> None:
    # Even repeated caller cancellation must await owned-process quiescence.
    await collected(asyncio.create_task(_close(host, rpc, grace)))


async def discover_models(
    config: CodexConfig, workspace: Path, *, command: Sequence[str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Read every visible model page from a fresh, version-checked metadata session.

    ``command`` and ``timeout`` allow deterministic fake-process tests. Discovery
    never opens/resumes a thread, starts a model turn, refreshes credentials, or
    falls back to a static/stale model list. The timeout bounds metadata work;
    owned-process cleanup is always awaited before returning or propagating errors.
    """
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("The model catalog timeout must be finite and positive")
    host: OwnedProcessHost | None = None
    rpc: JsonRpcClient | None = None
    try:
        try:
            async with asyncio.timeout(timeout):
                harness = CodexHarness(config, command=command)
                host = await OwnedProcessHost.start(
                    harness.command, cwd=workspace.resolve(), env=harness.process_environment(),
                    max_frame_bytes=config.max_frame_bytes, startup_timeout=min(timeout, 10.0),
                )
                rpc = JsonRpcClient(
                    host, max_frame_bytes=config.max_frame_bytes, request_timeout=timeout)
                user_agent = await initialize(rpc, "formal-geyser-model-catalog")
                if not audited_version(user_agent, config.version):
                    raise ModelCatalogError(
                        "model_catalog_version_mismatch",
                        "The Codex app-server version does not match the configured audited version. "
                        "Check the configured executable and version before refreshing the model catalog.",
                        status=503,
                    )
                await rpc.notify("initialized")
                authentication_required = _authentication_required(
                    await rpc.request("account/read", {"refreshToken": False}))
                return {
                    "models": await _models(rpc),
                    "authentication_required": authentication_required,
                    "source": "codex/model/list",
                }
        finally:
            if host is not None:
                await _finish_close(host, rpc, config.stop_grace_seconds)
    except (TimeoutError, RpcTimeout):
        raise ModelCatalogError(
            "model_catalog_timeout",
            "Codex model catalog discovery timed out. Check the configured Codex profile "
            "and connection, then refresh the catalog.",
            status=504,
        ) from None
    except RpcProtocolError:
        raise _invalid("app-server protocol") from None
    except (OSError, ProcessHostError, RpcError):
        # RPC failures may contain account data or credentials. Never expose their
        # raw messages, payloads, stderr, or exception chains to the API consumer.
        raise ModelCatalogError(
            "model_catalog_unavailable",
            "The Codex model catalog is unavailable. Check the configured executable "
            "and profile, sign in if required, then refresh the catalog.",
            status=503,
        ) from None
