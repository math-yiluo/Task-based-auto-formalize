"""Codex app-server harness pinned to an explicitly audited protocol version."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence
from uuid import uuid4

from ... import cgroups
from ..errors import HubError
from ..models import CodexConfig
from ..native_permissions import WORKER_PROFILE, worker_profile
from ..storage import collected, digest
from .diagnostics import normalize_error
from .executable import resolve_codex_executable
from .ownership import planned_receipt
from .process import OwnedProcessHost
from .rpc import JsonRpcClient, RpcClosed, RpcProtocolError, RpcRemoteError, RpcReply, RpcTimeout

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]
UsageHandler = Callable[[dict[str, Any]], Awaitable[None]]
StartedHandler = Callable[[str], Awaitable[None]]


def _tool_reply_value(result: dict[str, Any], success: bool) -> dict[str, Any]:
    return {
        "contentItems": [{"type": "inputText", "text": json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}],
        "success": success,
    }


def _hub_error_result(error: HubError) -> dict[str, Any]:
    """Keep recovery identities intact inside an 8 KiB serialized tool reply."""
    omitted = object()

    def detail(value: Any, depth: int, remaining: list[int]) -> Any:
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 4:
            return omitted
        if value is None or type(value) is bool:
            return value
        if type(value) is int and value.bit_length() <= 256:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) is str and len(value) <= 2048:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return omitted
            return value
        if type(value) is list and len(value) <= 32:
            sequence = [detail(item, depth + 1, remaining) for item in value]
            return omitted if any(item is omitted for item in sequence) else sequence
        if type(value) is dict and len(value) <= 32:
            if any(type(key) is not str or len(key) > 128
                   or detail(key, depth + 1, remaining) is omitted for key in value):
                return omitted
            items = {key: detail(item, depth + 1, remaining) for key, item in value.items()}
            return omitted if any(item is omitted for item in items.values()) else items
        return omitted

    def bounded_text(value: str, limit: int) -> str:
        encoded = value[:limit].encode("utf-8", errors="replace")
        text = encoded[:limit].decode("utf-8", errors="ignore")
        return text + " [truncated]" if len(text) < len(value) else text

    body: dict[str, Any] = {
        "code": bounded_text(error.code, 128),
        "message": bounded_text(error.message, 2000),
    }
    result = {"error": body}

    def size() -> int:
        return len(json.dumps(_tool_reply_value(result, False), ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False).encode("utf-8"))

    # Reserve room for recovery metadata even when JSON escapes expand the message.
    while size() > 4096:
        body["message"] = body["message"][:len(body["message"]) // 2] + " [truncated]"
    if error.details is not None:
        details: dict[str, Any] = {}
        body.update(details=details, details_truncated=True)
        # A large unrelated diagnostic must not displace the canonical recovery target.
        keys = dict.fromkeys(("current_revision", "current_graph_ref", *islice(error.details, 32)))
        for key in keys:
            if (key not in error.details or type(key) is not str or len(key) > 128
                    or detail(key, 0, [1]) is omitted):
                continue
            value = detail(error.details[key], 0, [128])
            if value is omitted:
                continue
            details[key] = value
            if size() > 8192:
                del details[key]
        if len(details) == len(error.details):
            body.pop("details_truncated")
    return result


@dataclass(frozen=True)
class SessionInfo:
    thread_id: str
    model: str
    config_hash: str


@dataclass(frozen=True)
class TurnOutcome:
    turn_id: str | None
    status: str
    terminal_observed: bool
    process_quiet: bool
    directive: str | None = None
    error: str | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()
    visible_output: str = ""
    final_output: str = ""


@dataclass(frozen=True)
class SteerOutcome:
    status: str
    turn_id: str | None = None
    error: str | None = None


class ToolResult(dict[str, Any]):
    def __init__(self, value: dict[str, Any], *, after_send: Callable[[], Awaitable[None]]):
        super().__init__(value)
        self.after_send = after_send


class Harness(Protocol):
    async def open(
        self, *, workspace: Path, thread_id: str | None,
        tools: Sequence[dict[str, Any]], instructions: str, read_only: bool = False,
    ) -> SessionInfo: ...

    async def run_turn(
        self, prompt: str, *, on_tool: ToolHandler, on_usage: UsageHandler,
        on_started: StartedHandler,
    ) -> TurnOutcome: ...

    async def close(self) -> None: ...

    @property
    def process_quiet(self) -> bool: ...


# Native notifications whose kind the activity log names; others are reported as "other".
REPORTED_ACTIVITY = frozenset({
    "item/agentMessage/delta", "item/reasoning/textDelta",
    "item/reasoning/summaryTextDelta", "item/started", "item/completed",
    "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
    "turn/started", "turn/completed", "thread/tokenUsage/updated", "thread/compacted",
})


async def initialize(rpc: JsonRpcClient, client: str, *, title: str | None = None,
                     timeout: float | None = None) -> str:
    """Open the app-server protocol as `client`; the server's user agent."""
    initialized = await rpc.request("initialize", {
        "clientInfo": {"name": client, **({"title": title} if title else {}), "version": "0.1.0"},
        "capabilities": {"experimentalApi": True},
    }, timeout=timeout)
    user_agent = initialized.get("userAgent", "") if isinstance(initialized, dict) else ""
    return user_agent if isinstance(user_agent, str) else ""


def audited_version(user_agent: str, version: str) -> bool:
    return re.search("/" + re.escape(version) + r"(?:\s|$)", user_agent) is not None


def sign_in_identity(home: Path) -> tuple[int, int, int] | None:
    """The Codex sign-in on disk, `auth.json`: it changes with each sign-in and token refresh."""
    try:
        info = (home / "auth.json").stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_ino, info.st_size


class CodexHarness:
    """One owned server activation; later activations resume the original thread."""

    # Nothing that acts outside the sandbox and no tool the agents never use; the user's own
    # (host) skills stay out of the project's conversations.
    disabled_features: tuple[str, ...] = ("multi_agent", "goals", "apps", "plugins", "hooks",
                                          "computer_use", "in_app_browser", "image_generation")
    enabled_features: tuple[str, ...] = ("skip_host_skill_discovery",)
    # Codex compaction keeps the conversation's user inputs verbatim and summarizes the rest.
    compaction_keeps_inputs = True
    # How long close() finishes the messages already received once the native process has
    # exited and its output ended (nothing more can arrive). In the Linux pilot a turn's terminal
    # waited behind usage records the congested coordination database took 12-58 s to accept;
    # dropped after 10 s, 29 interrupted turns a day were recorded with an unknown outcome.
    received_drain_seconds = 120.0

    def __init__(self, config: CodexConfig, *, command: Sequence[str] | None = None,
                 prepared_environment: dict[str, Any] | None = None, protected: Sequence[str] = (),
                 account: dict[str, str] | None = None):
        """`protected`: what sandboxed commands may not read (native_permissions.protected_reads).
        `account`: the Codex profile this activation signs in with (codex_profiles), its name and
        directory; its usage limits are recorded for it. Without one, the configured home."""
        self.config = config
        self.codex_profile: str | None = account["name"] if account else None
        self._account_home: str | None = account["home"] if account else None
        self._rate_limit_handler: Callable[[Any], Awaitable[None]] | None = None
        self.prepared_environment = deepcopy(prepared_environment)
        self.protected = list(protected)
        self.command = list(command) if command is not None else self.launch_command(config, self.protected)
        self.host: OwnedProcessHost | None = None
        self.rpc: JsonRpcClient | None = None
        self.session: SessionInfo | None = None
        self._turn_id: str | None = None
        self._binding_error: BaseException | None = None
        self._turn_terminal: asyncio.Future | None = None
        self._drain = asyncio.Event()
        self._directive: str | None = None
        self._binding_lock = asyncio.Lock()
        self._on_tool: ToolHandler | None = None
        self._on_usage: UsageHandler | None = None
        self._on_started: StartedHandler | None = None
        self._close_lock = asyncio.Lock()
        self._quiet = True
        self._on_compacted: Callable[[], Awaitable[None]] | None = None
        self._compaction_items: set[tuple[str, str]] = set()
        self._compaction_pairs: dict[str, list[int]] = {}
        self._activity_handler: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        self._last_activity = 0.0
        self._diagnostic_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._control_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._sign_in: tuple[int, int, int] | None = None  # The sign-in on disk when the process started.
        self._diagnostics: list[dict[str, Any]] = []
        self._last_error: str | None = None
        self._terminal_error: str | None = None
        self._tool_tasks: set[asyncio.Task] = set()
        self._tool_stop_requested = False
        self._native_handler: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        self._visible_messages: dict[str, str] = {}
        self._message_phases: dict[str, str | None] = {}
        self._output_schema: dict[str, Any] | None = None
        self._client_input_id: str | None = None
        self.session_path: str | None = None
        self._planned_cgroup: Path | None = None

    def plan_process(self) -> dict | None:
        """Name this activation's cgroup before launch, so recovery can probe it without an
        ownership record."""
        self._planned_cgroup = cgroups.root() / ("tree-" + uuid4().hex)
        return planned_receipt(self._planned_cgroup)

    @property
    def active_turn_id(self) -> str | None:
        if self._turn_terminal is None or self._turn_terminal.done():
            return None
        return self._turn_id

    def set_native_handler(
        self, handler: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._native_handler = handler

    def set_turn_options(
        self, *, output_schema: dict[str, Any] | None = None,
        client_input_id: str | None = None,
    ) -> None:
        self._output_schema = output_schema
        self._client_input_id = client_input_id

    @staticmethod
    def tool_execution_config(config: CodexConfig) -> dict[str, Any]:
        # Astra selects code_mode_only in its model catalog, overriding the old
        # feature flags. The audited namespace override keeps our existing flat
        # dynamic tools direct, so long handlers do not require model polling.
        return {
            "features.code_mode.direct_only_tool_namespaces": ["functions"],
            "features.code_mode.default_exec_yield_time_ms": 60000,
        }

    @classmethod
    def launch_command(cls, config: CodexConfig, protected: Sequence[str] = ()) -> list[str]:
        args = [resolve_codex_executable(config.executable)]
        if config.home is not None:
            args.extend(["-c", "sqlite_home=" + json.dumps(str(Path(config.home).expanduser().resolve()))])
        for key, value in cls.tool_execution_config(config).items():
            args.extend(["-c", key + "=" + json.dumps(value, separators=(",", ":"))])
        # Codex's app-server refuses permission profiles without a default; a thread that
        # names none (a decision helper's, which asks for the read-only sandbox) gets the
        # read-only built-in.
        args.extend(["-c", 'default_permissions=":read-only"', *worker_profile(protected)])
        for feature in cls.disabled_features:
            args.extend(["--disable", feature])
        for feature in cls.enabled_features:
            args.extend(["--enable", feature])
        return [*args, "app-server"]

    @property
    def ownership_receipt(self) -> dict | None:
        return self.host.ownership_receipt if self.host is not None else None

    def _prepared_environment_values(self) -> dict[str, str]:
        """Lean runs only in the project's Lean service (lean_check): the shell gets no Lean or
        Lake command, only the readable dependency sources."""
        if self.prepared_environment is None:
            return {}
        cache = self.prepared_environment.get("cache")
        if not isinstance(cache, dict):
            raise ValueError("Prepared Lean environment is missing its cache paths")

        def absolute(value: Any) -> str:
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise ValueError("Prepared Lean environment paths must be absolute")
            if os.pathsep in value:
                raise ValueError("Prepared Lean environment paths cannot contain the search-path separator")
            return str(Path(value).resolve())

        sources = cache.get("source_roots")
        if not isinstance(sources, list) or not sources:
            raise ValueError("Prepared Lean environment requires nonempty source_roots")
        inherited = next((value for key, value in os.environ.items() if key.upper() == "PATH"), "")
        # Directories with a Lean or Lake executable, such as elan's, stay out of the shell's PATH.
        tools = ("lean", "lake", "elan")
        path = [entry for entry in inherited.split(os.pathsep)
                if entry and not any((Path(entry) / tool).is_file() for tool in tools)]
        return {
            "PATH": os.pathsep.join(dict.fromkeys([str(Path(sys.executable).parent), *path])),
            "FORMAL_GEYSER_PYTHON": sys.executable,
            "LEAN_SRC_PATH": os.pathsep.join([".", *dict.fromkeys(absolute(value) for value in sources)]),
        }

    def shell_environment(self, environment: dict[str, str]) -> dict[str, str]:
        """A worker's shell environment, on every backend: a parent Lake or elan session never
        reaches it (Lean runs only in the Lean service), and Python uses UTF-8."""
        if self.prepared_environment is not None:
            environment = {key: value for key, value in environment.items()
                           if not (key.upper().startswith(("LEAN_", "LAKE_", "ELAN_"))
                                   or key.upper() in {"LEAN", "LAKE", "ELAN", "PATH"})}
            environment.update(self._prepared_environment_values())
        # UTF-8 whatever the host's locale.
        environment.setdefault("PYTHONUTF8", "1")
        return environment

    def codex_home(self) -> Path:
        """Where Codex keeps its sign-in: the profile's directory, the configured home, else
        Codex's own default."""
        if self._account_home is not None:
            return Path(self._account_home).expanduser().resolve()
        if self.config.home is not None:
            return Path(self.config.home).expanduser().resolve()
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

    def signed_in_again(self) -> bool:
        """Whether Codex was signed in again, or its tokens refreshed, after this process
        started. The process keeps the credentials it read then, and a new sign-in revokes
        them: its own sign-in errors say nothing about the current sign-in."""
        return self.host is not None and sign_in_identity(self.codex_home()) != self._sign_in

    def process_environment(self) -> dict[str, str] | None:
        if self.config.home is None and self.prepared_environment is None:
            return None
        environment = self.shell_environment(dict(os.environ))
        if self.config.home is not None:
            # A profile signs in from its own directory; every profile's databases are the
            # configured home's, so a thread continues under any of them.
            home = str(Path(self.config.home).expanduser().resolve())
            environment.update(CODEX_HOME=str(self.codex_home()), CODEX_SQLITE_HOME=home)
        return environment

    @property
    def process_quiet(self) -> bool:
        return (self._quiet and (self.host is None or self.host.tree_quiet)
                and all(task.done() for task in self._tool_tasks))

    async def open(
        self, *, workspace: Path, thread_id: str | None,
        tools: Sequence[dict[str, Any]], instructions: str, read_only: bool = False,
    ) -> SessionInfo:
        """Start a new thread, or resume the worker's own thread; threads are never forked."""
        if self.host is not None:
            raise RuntimeError("Harness activations cannot be reopened")
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        environment = self.process_environment()
        if self.config.home is not None and environment is not None:
            Path(environment["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
        self._sign_in = sign_in_identity(self.codex_home())
        rpc = await self._start_host(workspace, environment)
        effective: dict[str, Any] = {
            "protocol_version": self.config.version, "model": self.config.model,
            "effort": self.config.effort, "disabled_features": list(self.disabled_features),
            "enabled_features": list(self.enabled_features),
            "sandbox": "read-only" if read_only else None, "approval_policy": "never",
            "permissions": None if read_only else WORKER_PROFILE,
            "protected_reads": [] if read_only else list(self.protected),
            "workspace": str(workspace), "instructions": instructions,
            "codex_home": environment["CODEX_HOME"] if self.config.home is not None and environment else None,
            "codex_profile": self.codex_profile,
            "tool_execution": self.tool_execution_config(self.config),
        }
        self._record_environment(effective, thread_id, tools)
        try:
            user_agent = await initialize(rpc, "formal-geyser", title="FormalGeyser", timeout=120)
            if not audited_version(user_agent, self.config.version):
                raise RpcProtocolError(
                    f"Codex version does not match the audited {self.config.version}: {user_agent}")
            await rpc.notify("initialized")
            params: dict[str, Any] = {
                "cwd": str(workspace), "approvalPolicy": "never",
                "runtimeWorkspaceRoots": [str(workspace)],
                "developerInstructions": instructions,
                "config": {"model_reasoning_effort": self.config.effort,
                           **self.tool_execution_config(self.config)},
            }
            shell: dict[str, str] = {}
            if self.prepared_environment is not None and environment is not None:
                # Pin native shell tools as well as their app-server parent.
                shell.update({key: environment[key] for key in self._prepared_environment_values()})
            if read_only:
                params["sandbox"] = "read-only"
            else:
                # The same profile on starts and resumes: network access, writes only in this
                # workspace, and a temporary directory there, since /tmp is not writable.
                params["permissions"] = WORKER_PROFILE
                temporary = workspace / ".private" / "tmp"
                temporary.mkdir(parents=True, exist_ok=True)
                shell["TMPDIR"] = str(temporary)
            if shell:
                params["config"]["shell_environment_policy.set"] = shell
            if self.config.home is not None and environment is not None:
                params["config"]["sqlite_home"] = environment["CODEX_SQLITE_HOME"]
            if self.config.model is not None:
                params["model"] = self.config.model
            if thread_id is None:
                params["dynamicTools"] = list(tools)
                params["allowProviderModelFallback"] = False
                result = await rpc.request("thread/start", params)
            else:
                # Dynamic tools persist with the original thread.
                params["threadId"] = thread_id
                # Resume the full native context without copying its growing
                # visible history into one bounded RPC response.
                params["excludeTurns"] = True
                result = await rpc.request("thread/resume", params)
            session = self._bind_session(result, thread_id, effective, "unspecified")
            stored = result["thread"].get("path")
            self.session_path = stored if isinstance(stored, str) and stored else None
            return session
        except BaseException:
            await self._finish_close()
            raise

    async def _start_host(self, workspace: Path, environment: dict[str, str] | None) -> JsonRpcClient:
        """Launch the backend's server in the workspace, as an owned process, and connect to it."""
        self.host = await OwnedProcessHost.start(
            self.command, cwd=workspace, env=environment,
            max_frame_bytes=self.config.max_frame_bytes, cgroup=self._planned_cgroup)
        self._quiet = False
        self.rpc = JsonRpcClient(
            self.host, request_handler=self._request, notification_handler=self._notification,
            max_frame_bytes=self.config.max_frame_bytes)
        return self.rpc

    def _record_environment(self, effective: dict[str, Any], thread_id: str | None,
                            tools: Sequence[dict[str, Any]]) -> None:
        """The prepared environment and the tools a session's configuration hash covers."""
        if self.prepared_environment is not None:
            effective["prepared_environment"] = self.prepared_environment
        if thread_id:
            # A resume does not install the supplied definitions. Hash the persisted
            # catalog's thread without claiming to know its schemas.
            effective["tool_catalog"] = {"mode": "persisted", "source_thread_id": thread_id}
        else:
            effective["tools"] = list(tools)

    def _codex_home(self) -> Path:
        """Where Codex keeps its sessions: the configured home, else Codex's own default."""
        return Path(self.config.home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()

    def _bind_session(self, result: Any, thread_id: str | None, effective: dict[str, Any],
                      default_model: str) -> SessionInfo:
        """The session a thread/start or thread/resume reply describes; a resume keeps its thread."""
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise RpcProtocolError("Thread response did not include thread metadata")
        actual_id = result["thread"].get("id")
        if not isinstance(actual_id, str) or not actual_id:
            raise RpcProtocolError("Thread response did not include a thread ID")
        if thread_id is not None and actual_id != thread_id:
            raise RpcProtocolError("Resume changed the original thread identity")
        model = result.get("model") or self.config.model or default_model
        effective["model"] = model
        self.session = SessionInfo(actual_id, model, digest(effective))
        return self.session

    async def _bind(self, turn_id: str) -> None:
        async with self._binding_lock:
            if self._binding_error is not None:
                raise self._binding_error
            if self._turn_id is not None:
                if self._turn_id != turn_id:
                    raise RpcProtocolError("Received a different turn while one was active")
                return
            self._turn_id = turn_id
            if self._on_started is not None:
                try:
                    await self._on_started(turn_id)
                except BaseException as exc:
                    self._binding_error = exc
                    raise

    def set_diagnostic_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._diagnostic_handler = handler

    async def _record_error(
        self, error: Any, *, source: str, will_retry: bool | None = None,
    ) -> dict[str, Any]:
        record = normalize_error(error, source=source, will_retry=will_retry)
        if self._turn_id is not None:
            record["turn_id"] = self._turn_id
        self._last_error = record["message"]
        if source == "terminal":
            self._terminal_error = record["message"]
        if self._control_handler is not None:
            # Control signals such as an exhausted account are never subject to the log bound.
            await self._control_handler(record)
        if len(self._diagnostics) < 32:
            self._diagnostics.append(record)
            if self._diagnostic_handler is not None:
                await self._diagnostic_handler(record)
        return record

    def set_control_handler(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Receive every native error record, unlike the bounded diagnostic log."""
        self._control_handler = handler

    def set_activity_handler(
        self, handler: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._activity_handler = handler

    def set_compaction_handler(self, handler: Callable[[], Awaitable[None]]) -> None:
        self._on_compacted = handler

    async def _compacted(self, turn_id: str, item_id: str | None) -> None:
        await self._bind(turn_id)
        # Codex may emit the deprecated notification and a modern completed
        # item for the same compaction. Pair them in either arrival order.
        pending = self._compaction_pairs.setdefault(turn_id, [0, 0])
        own, other = (0, 1) if item_id is not None else (1, 0)
        if item_id is not None:
            identity = (turn_id, item_id)
            if identity in self._compaction_items:
                return
            self._compaction_items.add(identity)
        if pending[other]:
            pending[other] -= 1
            return
        pending[own] += 1
        if self._on_compacted is not None:
            await self._on_compacted()

    def set_rate_limit_handler(self, handler: Callable[[Any], Awaitable[None]]) -> None:
        """Receive each usage report of the account (account/rateLimits/updated)."""
        self._rate_limit_handler = handler

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "account/rateLimits/updated":
            # The account's, not a thread's: it names no thread.
            if self._rate_limit_handler is not None:
                try:
                    await self._rate_limit_handler(params.get("rateLimits"))
                except Exception as error:
                    # Usage is observation: a turn never fails for it.
                    logger.warning("Codex usage observation failed: %s", type(error).__name__)
            return
        if self.session is None or params.get("threadId") != self.session.thread_id:
            return
        if self._native_handler is not None:
            try:
                await self._native_handler(method, params)
            except Exception as error:
                # Recording is observation; control signals and the terminal are handled regardless.
                logger.warning("Native notification observation failed: %s", type(error).__name__)
        if self._turn_terminal is None:
            return
        if method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId"), params.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                self._visible_messages[item_id] = self._visible_messages.get(item_id, "") + delta
        elif method == "item/completed":
            item = params.get("item", {})
            if (item.get("type") == "agentMessage" and isinstance(item.get("id"), str)
                    and isinstance(item.get("text"), str)):
                self._visible_messages[item["id"]] = item["text"]
                self._message_phases[item["id"]] = item.get("phase")
        if (self._activity_handler is not None
                and time.monotonic() - self._last_activity >= 1.0):
            self._last_activity = time.monotonic()
            await self._activity_handler(
                "model_activity", {"method": method if method in REPORTED_ACTIVITY else "other"})
        if method == "error":
            if not isinstance(params.get("turnId"), str):
                raise RpcProtocolError("Error notification lacks its turn ID")
            await self._bind(params["turnId"])
            await self._record_error(
                params.get("error"), source="notification",
                will_retry=params.get("willRetry"))
        elif method == "turn/started":
            turn = params.get("turn", {})
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                await self._bind(turn["id"])
        elif method == "thread/compacted":
            if not isinstance(params.get("turnId"), str):
                raise RpcProtocolError("Compaction notification lacks its turn ID")
            await self._compacted(params["turnId"], None)
        elif method == "item/completed" and params.get("item", {}).get("type") == "contextCompaction":
            item_id, turn_id = params["item"].get("id"), params.get("turnId")
            if not isinstance(item_id, str) or not isinstance(turn_id, str):
                raise RpcProtocolError("Compaction item lacks its routing identifiers")
            await self._compacted(turn_id, item_id)
        elif method == "thread/tokenUsage/updated":
            if self._on_usage is not None:
                usage = params.get("tokenUsage", {}).get("total")
                if not isinstance(usage, dict):
                    raise RpcProtocolError("Token usage notification lacks cumulative total")
                last = params.get("tokenUsage", {}).get("last")
                await self._on_usage({**usage, "lastTokenUsage": last,
                                      "nativeTurnId": params.get("turnId")}
                                     if isinstance(last, dict) else usage)
        elif method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise RpcProtocolError("Malformed terminal turn notification")
            await self._bind(turn["id"])
            if turn.get("status") not in {"completed", "interrupted", "failed"}:
                raise RpcProtocolError("Turn completion has a nonterminal status")
            if turn.get("error") is not None:
                await self._record_error(turn["error"], source="terminal")
            elif turn.get("status") == "failed":
                await self._record_error(
                    {"message": self._last_error or "Codex reported a failed turn without error details"},
                    source="terminal")
            if not self._turn_terminal.done():
                self._turn_terminal.set_result(turn["status"])
            self._cancel_tool_callbacks()

    def _cancel_tool_callbacks(self) -> None:
        self._tool_stop_requested = True
        for task in self._tool_tasks:
            if not task.done() and not task.cancelling():
                task.cancel()

    async def _wait_tool_callbacks(self) -> None:
        if not self._tool_tasks:
            return
        pending = asyncio.gather(*self._tool_tasks, return_exceptions=True)
        try:
            async with asyncio.timeout(self.config.stop_grace_seconds):
                await asyncio.shield(pending)
        except TimeoutError as exc:
            raise TimeoutError("Dynamic tool cleanup did not finish before the shutdown deadline") from exc

    async def _request(self, method: str, params: dict[str, Any]) -> RpcReply:
        if method != "item/tool/call":
            # Never implicitly approve an unknown permission or interaction request.
            raise RpcProtocolError(f"Unsupported server request: {method}")
        if self.session is None or params.get("threadId") != self.session.thread_id:
            raise RpcProtocolError("Dynamic tool request targets the wrong thread")
        if self._on_tool is None or self._turn_terminal is None:
            raise RpcProtocolError("Dynamic tool request outside an active turn")
        turn_id = params.get("turnId")
        call_id = params.get("callId")
        name = params.get("tool")
        if not all(isinstance(x, str) and x for x in (turn_id, call_id, name)):
            raise RpcProtocolError("Dynamic tool request lacks its routing identifiers")
        assert isinstance(turn_id, str) and isinstance(call_id, str) and isinstance(name, str)
        await self._bind(turn_id)
        args = params.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = None
        cancelled = False
        if not isinstance(args, dict):
            result, success = {"error": "Tool arguments must be a JSON object"}, False
        elif self._directive is not None or self._tool_stop_requested:
            result, success = {"error": "Worker is draining; no further tool actions are allowed"}, False
            cancelled = self._tool_stop_requested
        else:
            handler = self._on_tool

            async def invoke() -> dict[str, Any]:
                return await handler(name, args, call_id)

            task = asyncio.create_task(invoke(), name="codex-dynamic-tool")
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
            try:
                result = await asyncio.shield(task)
                success = "error" not in result
            except asyncio.CancelledError:
                if not (task.cancelled() and self._tool_stop_requested):
                    raise
                result, success, cancelled = {"error": "Tool cancelled because the native turn is stopping"}, False, True
            except HubError as exc:
                result, success = _hub_error_result(exc), False
            except FileNotFoundError:
                result, success = {"code": "NOT_FOUND", "error": "The requested file or directory does not exist. Refresh its parent listing."}, False
            except Exception as exc:
                result, success = {"error": type(exc).__name__ + ": " + str(exc)[:2000]}, False
        directive = result.get("runtime_directive")
        after_send = getattr(result, "after_send", None)
        if directive in {"drain_for_wait", "drain_for_submission", "drain_for_release",
                         "drain_for_review"}:
            self._directive = directive

            admitted = after_send

            async def mark_draining() -> None:
                try:
                    if admitted is not None:
                        await admitted()
                finally:
                    self._drain.set()

            after_send = mark_draining
        # The driver consumes this control signal; the model receives only the
        # operation's task-facing result. Preserve the original result/callback.
        backstage = {"runtime_directive"}
        if result.get("material_path"):
            backstage.update({"material_sha256", "material_bytes"})
        visible = {key: value for key, value in result.items() if key not in backstage}
        original_ack = after_send

        async def retain_after_send() -> None:
            try:
                if original_ack is not None:
                    await original_ack()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # The reply was already delivered. A receipt-storage error must
                # not invalidate the tool result or terminate native work.
                try:
                    await self._record_error(error, source="tool_reply_acknowledgement")
                except Exception:
                    pass  # The original native conversation remains authoritative.

        return RpcReply(_tool_reply_value(visible, success), retain_after_send if after_send else None,
                        ignore_closed_receiver=cancelled)

    async def interrupt(self) -> None:
        if self.rpc is None or self.session is None or self._turn_id is None:
            return
        if self._turn_terminal is not None and self._turn_terminal.done():
            return
        self._cancel_tool_callbacks()
        await self.rpc.request("turn/interrupt", {
            "threadId": self.session.thread_id, "turnId": self._turn_id,
        }, timeout=self.config.stop_grace_seconds)

    async def steer(
        self, text: str, *, expected_turn_id: str, client_input_id: str,
    ) -> SteerOutcome:
        """Attempt one transport delivery; uncertain outcomes are never retried here."""
        if (self.rpc is None or self.session is None
                or self.active_turn_id != expected_turn_id):
            return SteerOutcome("deferred", expected_turn_id)
        try:
            result = await self.rpc.request("turn/steer", {
                "threadId": self.session.thread_id, "expectedTurnId": expected_turn_id,
                "clientUserMessageId": client_input_id,
                "input": [{"type": "text", "text": text}],
            })
            if not isinstance(result, dict) or result.get("turnId") != expected_turn_id:
                raise RpcProtocolError("turn/steer accepted a different or missing turn ID")
            return SteerOutcome("accepted", expected_turn_id)
        except RpcRemoteError as exc:
            # A JSON-RPC error is a definitive rejection; the inbox remains
            # pending for the next valid boundary. A timeout/EOF is different.
            return SteerOutcome("deferred", expected_turn_id, str(exc))
        except Exception as exc:
            return SteerOutcome("uncertain", expected_turn_id, str(exc))

    def recorded_input(self, client_input_id: str) -> tuple[str, str | None] | None:
        """Where this thread's stored session records a user input with this client ID, its
        timestamp and turn; otherwise None. A steer whose RPC timed out is often added later,
        once the running tool call returns. Reads only a session file inside the Codex home."""
        if not self.session_path:
            return None
        path = Path(self.session_path).resolve()
        home = self._codex_home()
        if not path.is_relative_to(home) or not path.is_file():
            return None
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if client_input_id not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                    continue
                payload = record["payload"]
                item = payload.get("item")
                if (isinstance(item, dict) and item.get("type") == "UserMessage"
                        and item.get("client_id") == client_input_id):
                    turn = payload.get("turn_id")
                    return str(record.get("timestamp") or "recorded"), turn if isinstance(turn, str) else None
        return None

    async def read_history(
        self, thread_id: str, *, workspace: Path, include_turns: bool = True,
    ) -> dict[str, Any]:
        """Read stored native history without thread/resume or turn/start."""
        if self.host is not None:
            raise RuntimeError("Use a new harness for read-only history inspection")
        workspace = workspace.resolve()
        self.host = await OwnedProcessHost.start(
            self.command, cwd=workspace, env=self.process_environment(),
            max_frame_bytes=self.config.max_frame_bytes)
        self._quiet = False
        self.rpc = JsonRpcClient(self.host, max_frame_bytes=self.config.max_frame_bytes)
        try:
            user_agent = await initialize(self.rpc, "formal-geyser-history", timeout=120)
            if not audited_version(user_agent, self.config.version):
                raise RpcProtocolError("Read-only history server version does not match the pinned runtime")
            await self.rpc.notify("initialized")
            for retry in range(3):
                try:
                    result = await self.rpc.request("thread/read", {
                        "threadId": thread_id, "includeTurns": include_turns}, timeout=120)
                    break
                except RpcTimeout:
                    if retry == 2:
                        raise
                    await asyncio.sleep(2 ** retry)
            if (not isinstance(result, dict) or not isinstance(result.get("thread"), dict)
                    or result["thread"].get("id") != thread_id):
                raise RpcProtocolError("thread/read returned a different or missing thread")
            return result
        finally:
            await self._finish_close()

    async def stored_tool_catalog(self, thread_id: str, *, workspace: Path) -> list[dict[str, Any]]:
        """Read full definitions from this thread's authoritative native metadata."""
        from .catalog import CatalogError, normalize_catalog

        result = await self.read_history(thread_id, workspace=workspace, include_turns=False)
        raw_path = result["thread"].get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise CatalogError("thread/read did not identify its stored session file")
        path = Path(raw_path).resolve()
        home = self._codex_home()
        if not path.is_relative_to(home) or not path.is_file():
            raise CatalogError("The stored session file is unavailable or outside the configured Codex home")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise CatalogError("Duplicate JSON field in stored session metadata")
                value[key] = item
            return value

        with path.open("r", encoding="utf-8") as source:
            for _ in range(20):
                line = source.readline(self.config.max_frame_bytes + 1)
                if not line or len(line.encode("utf-8")) > self.config.max_frame_bytes:
                    raise CatalogError("Stored session metadata is missing or exceeds the read limit")
                try:
                    record = json.loads(line, object_pairs_hook=unique_object)
                except ValueError as exc:
                    raise CatalogError("Stored session metadata contains malformed JSON") from exc
                if not isinstance(record, dict):
                    raise CatalogError("A stored session record is not an object")
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("id") != thread_id:
                    raise CatalogError("Stored session metadata does not match the requested thread ID")
                # Old sessions may have no dynamic tools. That is an empty
                # catalog, never evidence that today's tools were installed.
                catalog = payload.get("dynamic_tools")
                return normalize_catalog([] if catalog is None else catalog)
        raise CatalogError("No session metadata was found within the bounded session-file read")

    async def _terminal_after_transport_close(self, failure: BaseException) -> None:
        if isinstance(failure, RpcClosed) and self.rpc is not None:
            # The independent notification consumer may still hold a terminal
            # event and its final usage sample after the reader reaches EOF.
            await self.rpc.drain_callbacks(timeout=self.config.stop_grace_seconds)
            if self._turn_terminal is not None and self._turn_terminal.done():
                return
        raise failure

    async def run_turn(
        self, prompt: str, *, on_tool: ToolHandler, on_usage: UsageHandler,
        on_started: StartedHandler,
    ) -> TurnOutcome:
        if self.rpc is None or self.session is None:
            raise RuntimeError("Open the harness before starting a turn")
        if self._turn_terminal is not None:
            raise RuntimeError("One turn is permitted per harness activation")
        self._on_tool, self._on_usage, self._on_started = on_tool, on_usage, on_started
        self._turn_terminal = asyncio.get_running_loop().create_future()
        tasks: list[asyncio.Task] = []
        status, error = "unknown", None
        try:
            params: dict[str, Any] = {
                "threadId": self.session.thread_id,
                "input": [{"type": "text", "text": prompt}],
                "effort": self.config.effort,
            }
            if self._output_schema is not None:
                params["outputSchema"] = self._output_schema
            if self._client_input_id is not None:
                params["clientUserMessageId"] = self._client_input_id
            result = await self.rpc.request("turn/start", params, timeout=30.0)
            turn = result.get("turn", {}) if isinstance(result, dict) else {}
            if not isinstance(turn.get("id"), str):
                raise RpcProtocolError("turn/start did not return a turn ID")
            await self._bind(turn["id"])
            draining = asyncio.create_task(self._drain.wait())
            failed = asyncio.create_task(self.rpc.wait_failure())
            tasks = [draining, failed]
            done, _ = await asyncio.wait(
                [self._turn_terminal, draining, failed],
                return_when=asyncio.FIRST_COMPLETED)
            if failed in done:
                await self._terminal_after_transport_close(failed.result())
            if draining in done and not self._turn_terminal.done():
                await self.interrupt()
            if not self._turn_terminal.done():
                done, _ = await asyncio.wait(
                    [self._turn_terminal, failed],
                    timeout=self.config.stop_grace_seconds if draining in done else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if failed in done:
                    await self._terminal_after_transport_close(failed.result())
                if not self._turn_terminal.done():
                    raise TimeoutError("No terminal event after interruption")
            status = self._turn_terminal.result()
            self._cancel_tool_callbacks()
            await self._wait_tool_callbacks()
            await self.rpc.drain_callbacks(timeout=self.config.stop_grace_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record = await self._record_error(exc, source="transport")
            error = type(exc).__name__ + ": " + record["message"]
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._finish_close()
        terminal = self._turn_terminal.done() and not self._turn_terminal.cancelled()
        if terminal and status == "unknown" and self.process_quiet and self._binding_error is None:
            # The interruption wait may expire while a terminal is queued behind
            # callbacks. Successful close above proves EOF, callback drainage and
            # process quiescence; retain that late known outcome instead of
            # inventing an unknown paid turn. A failed close never reaches here.
            status = self._turn_terminal.result()
        if self._terminal_error is not None:
            error = error or self._terminal_error
        elif status in {"failed", "unknown"}:
            error = error or self._last_error
        finals = [text for item_id, text in self._visible_messages.items()
                  if self._message_phases.get(item_id) == "finalAnswer"]
        final_output = finals[-1] if finals else next(reversed(self._visible_messages.values()), "")
        return TurnOutcome(self._turn_id, status, terminal, self.process_quiet,
                           self._directive, error, tuple(self._diagnostics),
                           "\n".join(self._visible_messages.values()), final_output)

    async def _finish_close(self) -> None:
        # Repeated cancellation must not interrupt owned-process cleanup.
        await collected(asyncio.create_task(self.close()))

    async def close(self) -> None:
        async with self._close_lock:
            self._cancel_tool_callbacks()
            try:
                await self._wait_tool_callbacks()
                if self.rpc is not None:
                    # Let cancelled handlers answer while the native peer still exists; a backlog
                    # that outlasts the grace is finished below, after the peer has exited.
                    with suppress(TimeoutError):
                        await self.rpc.drain_callbacks(timeout=self.config.stop_grace_seconds)
            finally:
                try:
                    if self.host is not None:
                        await self.host.stop(grace=self.config.stop_grace_seconds)
                        self._quiet = self.host.tree_quiet
                    if self.rpc is not None:
                        await self.rpc.finish_receiving(timeout=self.config.stop_grace_seconds)
                        await self.rpc.drain_callbacks(
                            timeout=max(self.config.stop_grace_seconds, self.received_drain_seconds))
                finally:
                    if self.rpc is not None:
                        await self.rpc.close()


class DecisionHarness(CodexHarness):
    """A decision's conversation: no native tool but the command runner Codex 0.160 cannot
    turn off, in a read-only sandbox over an empty directory."""

    disabled_features = (*CodexHarness.disabled_features, "shell_tool", "browser_use", "browser_use_external")


@dataclass
class ScriptedAction:
    name: str
    arguments: dict[str, Any]


@dataclass
class ScriptedTurn:
    actions: Sequence[ScriptedAction]
    usage: dict[str, Any] | None = None
    status: str = "completed"
    visible_output: str = ""


class ScriptedHarness:
    """Deterministic harness for software integration; never starts a model."""

    compaction_keeps_inputs = True

    def __init__(self, turns: Sequence[ScriptedTurn] = (), *, model: str = "scripted"):
        self.turns = iter(turns)
        self.model = model
        self.session: SessionInfo | None = None
        self.process_quiet = True
        self.results: list[dict[str, Any]] = []
        self.opened_threads: list[str | None] = []
        self.workspaces: list[Path] = []
        self.prompts: list[str] = []

    async def open(
        self, *, workspace: Path, thread_id: str | None,
        tools: Sequence[dict[str, Any]], instructions: str, read_only: bool = False,
    ) -> SessionInfo:
        self.opened_threads.append(thread_id)
        self.workspaces.append(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        self.process_quiet = False
        self.session = SessionInfo(thread_id or "scripted-" + uuid4().hex, self.model, "scripted")
        return self.session

    async def run_turn(
        self, prompt: str, *, on_tool: ToolHandler, on_usage: UsageHandler,
        on_started: StartedHandler,
    ) -> TurnOutcome:
        turn_id = "scripted-turn-" + uuid4().hex
        directive = None
        self.prompts.append(prompt)
        try:
            turn = next(self.turns)
            await on_started(turn_id)
            if turn.usage is not None:
                await on_usage(turn.usage)
            for action in turn.actions:
                result = await on_tool(action.name, action.arguments, uuid4().hex)
                self.results.append(result)
                admitted = getattr(result, "after_send", None)
                if admitted is not None:
                    await admitted()
                directive = result.get("runtime_directive")
                if directive:
                    break
            return TurnOutcome(
                turn_id, "interrupted" if directive else turn.status, True, True, directive,
                visible_output=turn.visible_output, final_output=turn.visible_output)
        except StopIteration:
            return TurnOutcome(turn_id, "failed", True, True, error="Script exhausted")
        finally:
            await self.close()

    async def close(self) -> None:
        self.process_quiet = True



