"""Claude Code workers through the pinned Agent SDK, billed only to a Claude subscription.

Each activation starts ``claude_bridge`` as an owned process tree. The bridge speaks the
app-server subset that CodexHarness already drives (turns, tool calls, usage, compaction,
interruption), so turn handling, tool replies, and shutdown are shared with Codex.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from ..faults import ACCOUNT_HOLDS
from ..models import ClaudeConfig
from .claude_bridge import PROTOCOL, BridgeError, read_session, subscription_environment
from .codex import CodexHarness, SessionInfo
from .rpc import RpcProtocolError, RpcRemoteError

logger = logging.getLogger(__name__)
BRIDGE_MODULE = "formal_geyser.coordination.runtime.claude_bridge"
# Starting Claude Code includes a first-run binary check and its own handshake.
STARTUP_TIMEOUT = 180.0
CLAUDE_TOOL_NOTE = (
    "\nTaskHub operations are the MCP tools mcp__taskhub__taskhub_actions and mcp__taskhub__taskhub_call. "
    "Shell commands run in Claude Code's sandbox: write only inside your workspace, never ask to run a "
    "command outside the sandbox, and run each command in the foreground until it finishes. Directed "
    "messages are not added to a running turn; during long work call the sync operation to receive them. "
    "Subagents, web tools, skills, and slash commands are unavailable; fetch sources with curl. Writing the "
    "required Markdown files (paired .md files, notes, manifests) is part of the work.")


def claude_unavailable(config: ClaudeConfig) -> str | None:
    """Why Claude workers cannot start on this machine, or None."""
    from ..native_permissions import INTEROP, wsl_interop

    if sys.platform != "linux":
        return "Claude workers run on Linux, where Claude Code's command sandbox exists."
    release = platform.release().lower()
    if "microsoft" in release and "wsl2" not in release:
        return "Claude workers need WSL 2: Claude Code's command sandbox does not run on WSL 1."
    if wsl_interop():
        return INTEROP + "."
    missing = [name for name in ("bwrap", "socat") if shutil.which(name) is None]
    if missing:
        return "Install " + " and ".join(missing) + ": Claude Code's command sandbox needs them."
    if importlib.util.find_spec("claude_agent_sdk") is None:
        return "Install the claude extra (claude-agent-sdk) before selecting Claude workers."
    if not config.home:
        return "Configure claude.home: the directory that holds the Claude subscription sign-in."
    return None


class ClaudeHarness(CodexHarness):
    """One owned Claude Code activation; later activations resume the original session."""

    config: ClaudeConfig  # type: ignore[assignment]
    # Claude Code cannot add input to a running turn. Context refreshes reach the model
    # through tool results and the next continuation instead.
    steer = None  # type: ignore[assignment]
    disabled_features = ()
    # Claude Code's compaction summarizes the conversation's inputs too: the next tool result
    # brings the current task view again.
    compaction_keeps_inputs = False
    # The subscription profile this activation signs in with (claude_profiles); its usage limits
    # are reported for it.
    claude_profile: str | None = None

    def __init__(self, config: ClaudeConfig, *, command: Sequence[str] | None = None,
                 prepared_environment: dict[str, Any] | None = None, protected: Sequence[str] = ()):
        super().__init__(config, command=command, prepared_environment=prepared_environment,  # type: ignore[arg-type]
                         protected=protected)
        self._rate_limit_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def set_rate_limit_handler(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Receive each change of a usage window's state that Claude Code reports."""
        self._rate_limit_handler = handler

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "account/rateLimit":
            await super()._notification(method, params)
            return
        if (self._rate_limit_handler is not None and self.session is not None
                and params.get("threadId") == self.session.thread_id):
            try:
                await self._rate_limit_handler(params)
            except Exception as error:
                # Usage is observation: a turn never fails for it.
                logger.warning("Claude usage observation failed: %s", type(error).__name__)

    @staticmethod
    def tool_execution_config(config: Any) -> dict[str, Any]:
        return {}

    @classmethod
    def launch_command(cls, config: Any, protected: Sequence[str] = ()) -> list[str]:
        return [sys.executable, "-I", "-m", BRIDGE_MODULE]

    def _home(self) -> Path:
        if not self.config.home:
            raise ValueError("claude.home must name the directory that holds the Claude subscription sign-in")
        return Path(self.config.home).expanduser().resolve()

    def process_environment(self) -> dict[str, str]:
        """The bridge's environment: no API-billing credential, only the configured sign-in home."""
        environment = self.shell_environment(subscription_environment(dict(os.environ)))
        environment["CLAUDE_CONFIG_DIR"] = str(self._home())
        environment["FORMAL_GEYSER_BRIDGE_FRAME_BYTES"] = str(self.config.max_frame_bytes)
        return environment

    async def _hold_for_account(self, error: RpcRemoteError) -> None:
        body: dict[str, Any] = error.error if isinstance(error.error, dict) else {}
        data = body.get("data")
        code = data.get("code") if isinstance(data, dict) else None
        if isinstance(code, str) and code in ACCOUNT_HOLDS:
            # An unusable subscription stops Claude dispatch exactly like a Codex account error.
            await self._record_error({"code": code, "message": str(body.get("message", ""))},
                                     source="preflight")

    async def open(
        self, *, workspace: Path, thread_id: str | None,
        tools: Sequence[dict[str, Any]], instructions: str, read_only: bool = False,
    ) -> SessionInfo:
        """Start a new session, or resume the worker's own session; sessions are never forked."""
        if self.host is not None:
            raise RuntimeError("Harness activations cannot be reopened")
        if read_only or self._output_schema is not None:
            raise RuntimeError("A Claude session is a worker's own session; decisions run on Codex")
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        environment = self.process_environment()
        Path(environment["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
        rpc = await self._start_host(workspace, environment)
        effective: dict[str, Any] = {
            "backend": "claude", "sdk_version": self.config.sdk_version,
            "cli_version": self.config.cli_version, "executable": self.config.executable,
            "model": self.config.model, "effort": self.config.effort,
            "allowed_domains": list(self.config.allowed_domains), "protected_reads": list(self.protected),
            "workspace": str(workspace), "instructions": instructions,
            "claude_home": environment["CLAUDE_CONFIG_DIR"], "claude_profile": self.claude_profile,
        }
        self._record_environment(effective, thread_id, tools)
        try:
            initialized = await rpc.request("initialize", {
                "clientInfo": {"name": "formal-geyser", "title": "FormalGeyser", "version": "0.1.0"},
            }, timeout=120)
            if (not isinstance(initialized, dict) or initialized.get("protocol") != PROTOCOL
                    or initialized.get("sdkVersion") != self.config.sdk_version):
                found = initialized.get("sdkVersion") if isinstance(initialized, dict) else None
                raise RpcProtocolError(
                    f"Claude Agent SDK {found} does not match the audited {self.config.sdk_version}")
            await rpc.notify("initialized")
            params: dict[str, Any] = {
                "cwd": str(workspace), "instructions": instructions,
                "model": self.config.model, "effort": self.config.effort,
                "allowedDomains": list(self.config.allowed_domains), "denyRead": list(self.protected),
                "cliPath": self.config.executable, "cliVersion": self.config.cli_version,
                "maxFrameBytes": self.config.max_frame_bytes,
            }
            if thread_id is None:
                method = "thread/start"
                params["tools"] = list(tools)
            else:
                method = "thread/resume"
                params["threadId"] = thread_id
            try:
                result = await rpc.request(method, params, timeout=STARTUP_TIMEOUT)
            except RpcRemoteError as error:
                await self._hold_for_account(error)
                raise
            return self._bind_session(result, thread_id, effective, "claude-default")
        except BaseException:
            await self._finish_close()
            raise

    async def read_history(self, thread_id: str, *, workspace: Path, include_turns: bool = True) -> dict[str, Any]:
        record = read_session(self._home(), thread_id)
        return {"thread": {"id": thread_id, "turns": record.get("turns", []) if include_turns else []}}

    async def stored_tool_catalog(self, thread_id: str, *, workspace: Path) -> list[dict[str, Any]]:
        """Full definitions recorded when the session started, which every activation reinstalls."""
        from .catalog import CatalogError, normalize_catalog

        try:
            record = read_session(self._home(), thread_id)
        except (BridgeError, ValueError) as exc:
            raise CatalogError(str(exc)) from exc
        return normalize_catalog(record.get("tools") or [])
