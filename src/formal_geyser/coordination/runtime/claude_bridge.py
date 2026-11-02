"""Claude Code peer for one owned activation, speaking the app-server subset the harness audits.

The harness starts this module in a cgroup of its own; the Claude Code CLI started here
runs in it too. No credential is read or forwarded: the CLI uses the subscription sign-in stored
under CLAUDE_CONFIG_DIR, every API-billing source is removed from its environment, and no
prompt is sent until the CLI reports a subscription account.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, uuid4

PROTOCOL = 1
SERVER = "taskhub"
PROJECT_DIR_NAME = "task-based"
# About 23 days: like Codex turns and checks, commands have no practical elapsed-time limit.
NO_LIMIT_MS = "2000000000"
LOCAL_HOSTS = ["localhost", "*.localhost", "127.0.0.1", "0.0.0.0", "[::1]"]
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READ_TOOLS = ["Read", "Glob", "Grep"]
WORK_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]
DENIED_TOOLS = ["Agent", "Task", "AskUserQuestion", "Skill", "SlashCommand"]
# TaskHub replies stay inline; their packets are far below this.
RESULT_CHARS = 200000
API_BILLING_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
API_BILLING_KEYS = frozenset({"AWS_BEARER_TOKEN_BEDROCK", "CLAUDECODE"})
CLI_ENVIRONMENT = {
    "CLAUDE_CODE_PROJECT_DIR_NAME": PROJECT_DIR_NAME,
    "BASH_DEFAULT_TIMEOUT_MS": NO_LIMIT_MS,
    "BASH_MAX_TIMEOUT_MS": NO_LIMIT_MS,
    "MCP_TOOL_TIMEOUT": NO_LIMIT_MS,
    # Every command finishes inside its turn, as the turn-end rule requires.
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    # Workers share one configuration directory; nothing may carry between their sessions.
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    # Fast mode is paid from usage credits, never from the plan's included usage.
    "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
    # Skips the background request that titles a session.
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    # The audited binary is never replaced underneath a run.
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}
# Final assistant errors that stop dispatch to Claude workers (see faults.ACCOUNT_HOLDS).
ACCOUNT_ERRORS = {"authentication_failed": "claudeSignInRequired", "billing_error": "claudeUsageLimit",
                  "rate_limit": "claudeUsageLimit"}


class BridgeError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def subscription_environment(environment: dict[str, str]) -> dict[str, str]:
    """Remove every credential source that would bill an API account instead of the subscription."""
    return {key: value for key, value in environment.items()
            if not key.upper().startswith(API_BILLING_PREFIXES) and key.upper() not in API_BILLING_KEYS}


def _session_id(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError as exc:
        raise BridgeError("Claude session identities are UUIDs") from exc


def session_path(home: Path, session_id: str) -> Path:
    return home / "task-based" / "sessions" / (_session_id(session_id) + ".json")


def read_session(home: Path, session_id: str) -> dict[str, Any]:
    try:
        record = json.loads(session_path(home, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError("No TaskHub record exists for this Claude session") from exc
    if not isinstance(record, dict) or record.get("session_id") != session_id or record.get("format") != 1:
        raise BridgeError("The TaskHub record for this Claude session is malformed")
    return record


def write_session(home: Path, record: dict[str, Any]) -> None:
    """Replace atomically: a concurrent reader always sees one complete version."""
    path = session_path(home, record["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def transcript_path(home: Path, session_id: str) -> Path | None:
    exact = home / "projects" / PROJECT_DIR_NAME / (_session_id(session_id) + ".jsonl")
    if exact.is_file():
        return exact
    return next(iter(sorted((home / "projects").glob("*/" + session_id + ".jsonl"))), None)


def prompt_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()



def usage_totals(usage: dict[str, Any] | None) -> dict[str, int]:
    """Claude token counts in the harness's cumulative shape; cache reads count as input."""
    usage = usage if isinstance(usage, dict) else {}

    def number(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                return value
        return 0
    fresh = number("input_tokens", "inputTokens")
    read = number("cache_read_input_tokens", "cacheReadInputTokens")
    created = number("cache_creation_input_tokens", "cacheCreationInputTokens")
    output = number("output_tokens", "outputTokens")
    return {"totalTokens": fresh + read + created + output, "inputTokens": fresh + read + created,
            "cachedInputTokens": read, "cacheCreationInputTokens": created, "outputTokens": output}


def _add(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {key: left.get(key, 0) + right.get(key, 0) for key in set(left) | set(right)}


def _rule(tool: str, path: Path, *, tree: bool = False) -> str:
    # Permission rules spell an absolute path with a leading //.
    return f"{tool}(//{path.as_posix().lstrip('/')}{'/**' if tree else ''})"


def settings_for(*, workspace: Path, home: Path, allowed_domains: list[str],
                 protected: Sequence[str] = ()) -> dict[str, Any]:
    """Permission rules and the OS sandbox for one session; nothing is loaded from files.
    Nothing reads the sign-in or the `protected` paths (native_permissions.protected_reads)."""
    allow = [*READ_TOOLS, "TodoWrite", f"mcp__{SERVER}__taskhub_actions", f"mcp__{SERVER}__taskhub_call",
             "Bash", _rule("Edit", workspace, tree=True)]
    network: dict[str, Any] = {"deniedDomains": list(LOCAL_HOSTS)}
    if "*" in allowed_domains:
        # A bare-* WebFetch rule pre-allows every host for sandboxed commands; the
        # WebFetch tool itself is not in the session's tool set.
        allow.append("WebFetch(domain:*)")
    named = [domain for domain in allowed_domains if domain != "*"]
    if named:
        network["allowedDomains"] = named
    denied = [Path(path) for path in dict.fromkeys([str(home / ".credentials.json"), *protected])]
    return {
        "permissions": {
            "allow": allow,
            "deny": [*(_rule("Read", path, tree=path.is_dir()) for path in denied), *DENIED_TOOLS],
            "defaultMode": "dontAsk",
            "disableBypassPermissionsMode": "disable",
        },
        "sandbox": {
            "enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False, "network": network,
            "filesystem": {"denyRead": [str(path) for path in denied]},
        },
    }


def shown_schema(spec: dict[str, Any]) -> dict[str, Any]:
    """The input schema Claude Code is shown for a TaskHub tool, and checks input against.

    Claude Code refuses input that fails this schema before the bridge sees it. Claude often
    writes an operation's fields beside `operation`, or `arguments` as a JSON string (88 refused
    calls in a day of the pilot), so taskhub_call is shown a lenient copy and gateway_call
    restores the exact shape. The session record keeps the exact definitions, which the
    runner's catalog check compares with its own; only what Claude Code is shown differs.
    """
    schema = deepcopy(spec.get("inputSchema") or {"type": "object", "properties": {}})
    if spec.get("name") != "taskhub_call":
        return schema
    schema["required"] = [name for name in schema.get("required", []) if name != "arguments"]
    schema["additionalProperties"] = True
    arguments = schema.get("properties", {}).get("arguments")
    if isinstance(arguments, dict):
        arguments.update(type=["object", "string"], description="The operation's arguments object.")
    return schema


def gateway_call(call: dict[str, Any]) -> dict[str, Any]:
    """taskhub_call input in its exact shape: an `arguments` object (empty if omitted, parsed if
    written as JSON text) holding every field written beside `operation`."""
    beside = {key: value for key, value in call.items() if key not in {"operation", "arguments"}}
    arguments = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            raise ValueError("Invalid arguments: write arguments as a JSON object of the operation's fields, "
                             "not as text.")
    if beside:
        if not isinstance(arguments, dict):
            raise ValueError("Invalid arguments: put the operation's fields in the arguments object.")
        clashes = [key for key in beside if key in arguments and arguments[key] != beside[key]]
        if clashes:
            raise ValueError("Invalid arguments: give each field once, inside arguments: "
                             + ", ".join(key[:40] for key in clashes[:6]) + ".")
        arguments = {**beside, **arguments}
    return {**({"operation": call["operation"]} if "operation" in call else {}), "arguments": arguments}


class Peer:
    """Newline-delimited JSON-RPC on stdio, in the framing of rpc.JsonRpcClient."""

    def __init__(self, handler, *, max_frame_bytes: int):
        self.handler = handler
        self.max_frame_bytes = max_frame_bytes
        self._loop = asyncio.get_running_loop()
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._outgoing: asyncio.Queue = asyncio.Queue()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._tasks: set[asyncio.Task] = set()
        threading.Thread(target=self._read, name="bridge-stdin", daemon=True).start()
        self._writer = asyncio.create_task(self._write(), name="bridge-stdout")

    def _read(self) -> None:
        stream = sys.stdin.buffer
        while True:
            try:
                line = stream.readline(self.max_frame_bytes + 1)
            except (OSError, ValueError):
                line = b""
            self._loop.call_soon_threadsafe(self._incoming.put_nowait, line or None)
            if not line:
                return

    async def _write(self) -> None:
        def emit(frame: bytes) -> None:
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()
        while True:
            frame, done = await self._outgoing.get()
            try:
                await asyncio.to_thread(emit, frame)
                done.set_result(None)
            except BaseException as exc:
                if not done.done():
                    done.set_exception(exc)
                if not isinstance(exc, Exception):
                    raise

    async def send(self, message: dict[str, Any]) -> None:
        frame = (json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if len(frame) > self.max_frame_bytes:
            raise BridgeError("Outgoing frame exceeds the byte limit")
        done = self._loop.create_future()
        await self._outgoing.put((frame, done))
        await done

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future = self._loop.create_future()
        self._pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _answer(self, message: dict[str, Any]) -> None:
        try:
            result = await self.handler(message["method"], message.get("params") or {})
            reply: dict[str, Any] = {"id": message["id"], "result": result}
        except BridgeError as exc:
            error: dict[str, Any] = {"code": -32000, "message": str(exc)[:4000]}
            if exc.code:
                error["data"] = {"code": exc.code}
            reply = {"id": message["id"], "error": error}
        except Exception as exc:
            reply = {"id": message["id"], "error": {"code": -32603,
                                                    "message": (type(exc).__name__ + ": " + str(exc))[:4000]}}
        await self.send(reply)

    async def serve(self) -> None:
        """Answer requests concurrently until stdin closes."""
        while True:
            line = await self._incoming.get()
            if line is None:
                return
            if len(line) > self.max_frame_bytes or not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                task = asyncio.create_task(self._answer(message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif "method" in message:
                continue  # "initialized" carries nothing this peer needs.
            else:
                request_id = message.get("id")
                future = self._pending.get(request_id) if isinstance(request_id, int) else None
                if future is None or future.done():
                    continue  # The awaiting call was cancelled with its turn.
                if "error" in message:
                    future.set_exception(BridgeError(str(message["error"].get("message", "Remote error"))
                                                     if isinstance(message["error"], dict) else "Remote error"))
                else:
                    future.set_result(message.get("result"))


class Turn:
    def __init__(self, turn_id: str, base: dict[str, int]):
        self.id = turn_id
        self.base = dict(base)
        self.messages: dict[str, dict[str, int]] = {}
        self.reported = 0
        self.interrupt_reason: str | None = None
        self.items: dict[str, dict[str, Any]] = {}
        self.task: asyncio.Task | None = None


class Bridge:
    def __init__(self, sdk: Any):
        self.sdk = sdk
        self.peer: Peer | None = None
        self.home = Path(os.environ.get("CLAUDE_CONFIG_DIR", "")).expanduser()
        self.client: Any = None
        self.session: dict[str, Any] | None = None
        self.record: dict[str, Any] | None = None
        self.turn: Turn | None = None
        self.params: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.stderr: deque[str] = deque(maxlen=64)
        self._calls: dict[tuple[str, str], deque[str]] = {}
        self._closing = asyncio.Event()
        self._lifecycle: asyncio.Task | None = None
        self._connected: asyncio.Future | None = None

    # -- protocol ---------------------------------------------------------------------

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize()
        if method in {"thread/start", "thread/resume"}:
            return await self._open(method, params)
        if method == "turn/start":
            return await self._turn_start(params)
        if method == "turn/interrupt":
            return await self._interrupt(params, "operator")
        raise BridgeError(f"Unsupported request: {method}")

    def _initialize(self) -> dict[str, Any]:
        if not self.home.is_absolute():
            raise BridgeError("CLAUDE_CONFIG_DIR must name the signed-in Claude configuration directory")
        sdk_version = getattr(self.sdk, "__version__", "unknown")
        cli_version = getattr(getattr(self.sdk, "_cli_version", None), "__cli_version__", None)
        return {"userAgent": f"formal-geyser-claude-bridge/{PROTOCOL} claude-agent-sdk/{sdk_version}",
                "protocol": PROTOCOL, "sdkVersion": sdk_version, "bundledCliVersion": cli_version}

    async def _cli_version(self, executable: str | None) -> str | None:
        if not executable:
            return getattr(getattr(self.sdk, "_cli_version", None), "__cli_version__", None)
        try:
            process = await asyncio.create_subprocess_exec(
                executable, "-v", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, **CLI_ENVIRONMENT, "CLAUDE_CONFIG_DIR": str(self.home)})
            output, _ = await asyncio.wait_for(process.communicate(), 60)
        except (OSError, TimeoutError) as exc:
            raise BridgeError("Claude Code could not report its version: " + (str(exc) or type(exc).__name__)) from exc
        match = re.search(r"\d+\.\d+\.\d+", output.decode("utf-8", errors="replace"))
        return match.group(0) if match else None

    async def _open(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.session is not None:
            raise BridgeError("One session is opened per activation")
        expected = params.get("cliVersion")
        actual = await self._cli_version(params.get("cliPath"))
        if expected and actual != expected:
            raise BridgeError(f"Claude Code {actual or 'of unknown version'} does not match the audited {expected}")
        workspace = Path(params["cwd"]).resolve()
        self.workspace, self.params = workspace, params
        now = int(time.time() * 1000)
        launch: dict[str, Any]
        if method == "thread/start":
            session_id = str(uuid4())
            record = {"format": 1, "session_id": session_id, "created_at": now, "cwd": str(workspace),
                      "source": None, "tools": list(params.get("tools") or []), "turns": [],
                      "usage": usage_totals(None)}
            launch = {"session_id": session_id}
            history: dict[str, Any] = {"id": session_id, "turns": []}
        else:
            session_id = _session_id(params["threadId"])
            record = read_session(self.home, session_id)
            if transcript_path(self.home, session_id) is None:
                raise BridgeError("This Claude session was never saved; start a new one")
            launch = {"resume": session_id}
            history = {"id": session_id, "turns": []}
        self.session = {"id": session_id, "launch": launch}
        self.record = record
        try:
            await self._connect(launch)
        except BaseException:
            # Nothing was sent; stop Claude Code now rather than at the harness's close.
            self._closing.set()
            if self._lifecycle is not None:
                await asyncio.gather(self._lifecycle, return_exceptions=True)
            raise
        write_session(self.home, record)
        return {"thread": history, "model": params.get("model") or "claude-default"}

    def _options(self, launch: dict[str, Any]) -> Any:
        params, workspace = self.params, self.workspace
        assert workspace is not None and self.session is not None and self.record is not None
        tools = WORK_TOOLS
        server = self.sdk.create_sdk_mcp_server(name=SERVER, version="1", tools=[
            self._mcp_tool(spec) for spec in self.record.get("tools", [])])
        temporary = Path("/tmp") / ("tbc-" + self.session["id"].replace("-", "")[:12])
        temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = {**CLI_ENVIRONMENT, "CLAUDE_CONFIG_DIR": str(self.home),
                       "CLAUDE_CODE_TMPDIR": str(temporary)}
        protected = [path for path in params.get("denyRead") or [] if isinstance(path, str) and Path(path).is_absolute()]
        settings = settings_for(workspace=workspace, home=self.home,
                                allowed_domains=list(params.get("allowedDomains") or []), protected=protected)
        options: dict[str, Any] = {
            "cwd": str(workspace), "tools": list(tools),
            "allowed_tools": [name for name in tools if name not in WRITE_TOOLS]
            + [f"mcp__{SERVER}__taskhub_actions", f"mcp__{SERVER}__taskhub_call"],
            "disallowed_tools": list(DENIED_TOOLS),
            "permission_mode": "dontAsk",
            "system_prompt": {"type": "preset", "preset": "claude_code", "append": params.get("instructions", "")},
            "setting_sources": [], "strict_mcp_config": True, "mcp_servers": {SERVER: server},
            "settings": json.dumps(settings, separators=(",", ":")),
            "hooks": {"PreToolUse": [self.sdk.HookMatcher(matcher=None, hooks=[self._pre_tool_use])]},
            "env": environment, "verbatim_prompts": True,
            "max_buffer_size": int(params.get("maxFrameBytes") or 67108864),
            "stderr": self.stderr.append,
            **launch,
        }
        if params.get("model"):
            options["model"] = params["model"]
        if params.get("effort"):
            options["effort"] = params["effort"]
        if params.get("cliPath"):
            options["cli_path"] = params["cliPath"]
        return self.sdk.ClaudeAgentOptions(**options)

    async def _connect(self, launch: dict[str, Any]) -> None:
        """Start Claude Code and confirm a subscription account before anything is sent."""
        self.client = self.sdk.ClaudeSDKClient(self._options(launch))
        self._connected = asyncio.get_running_loop().create_future()
        # connect() and disconnect() share one task: the SDK's task group is bound to it.
        self._lifecycle = asyncio.create_task(self._own_client(), name="claude-client")
        await asyncio.shield(self._connected)
        info = await self.client.get_server_info() or {}
        account = info.get("account") if isinstance(info, dict) else None
        account = account if isinstance(account, dict) else {}
        if account.get("apiKeySource") not in {None, "none"} or not account.get("subscriptionType"):
            raise BridgeError("Claude Code is not signed in with a subscription account; API billing is disabled",
                              "claudeSubscriptionRequired" if account.get("apiKeySource") not in {None, "none"}
                              else "claudeSignInRequired")

    async def _own_client(self) -> None:
        assert self._connected is not None
        try:
            await self.client.connect()
        except BaseException as exc:
            if not self._connected.done():
                tail = " | ".join(list(self.stderr)[-5:])
                self._connected.set_exception(BridgeError(
                    "Claude Code did not start: " + (str(exc) or type(exc).__name__) + (" | " + tail if tail else "")))
            return
        self._connected.set_result(None)
        try:
            await self._closing.wait()
        finally:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    def _mcp_tool(self, spec: dict[str, Any]) -> Any:
        name = spec["name"]

        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return await self._call_tool(name, arguments)
        return self.sdk.SdkMcpTool(
            name=name, description=spec.get("description", ""), input_schema=shown_schema(spec),
            handler=handler, annotations=self.sdk.ToolAnnotations(maxResultSizeChars=RESULT_CHARS))

    @staticmethod
    def _call_key(name: str, arguments: Any) -> tuple[str, str]:
        return name, json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)

    @staticmethod
    def _error(message: str, code: str | None = None) -> dict[str, Any]:
        body = {"error": message, **({"code": code} if code else {})}
        return {"content": [{"type": "text", "text": json.dumps(body)}], "is_error": True}

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        turn = self.turn
        if turn is None or self.peer is None or self.session is None:
            return self._error("No turn is active")
        # The hook saw the input as Claude Code passed it, which is what identifies the call.
        waiting = self._calls.get(self._call_key(name, arguments))
        call_id = waiting.popleft() if waiting else "call-" + uuid4().hex
        if name == "taskhub_call":
            try:
                arguments = gateway_call(arguments)
            except ValueError as exc:
                return self._error(str(exc), "INVALID_ARGUMENTS")
        try:
            reply = await self.peer.request("item/tool/call", {
                "threadId": self.session["id"], "turnId": turn.id, "callId": call_id,
                "tool": name, "arguments": arguments})
        except BridgeError as exc:
            return self._error(str(exc))
        items = reply.get("contentItems") if isinstance(reply, dict) else None
        text = "".join(item.get("text", "") for item in items or [] if isinstance(item, dict))
        return {"content": [{"type": "text", "text": text}],
                "is_error": not (isinstance(reply, dict) and reply.get("success") is True)}

    async def _pre_tool_use(self, data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        """The authoritative check before any tool runs, whatever the permission rules say."""
        name = str(data.get("tool_name") or "")
        raw_input = data.get("tool_input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        prefix = f"mcp__{SERVER}__"
        if name.startswith(prefix):
            if tool_use_id:
                self._calls.setdefault(self._call_key(name[len(prefix):], tool_input), deque()).append(tool_use_id)
            return {}
        if name not in WORK_TOOLS:
            return self._deny(f"{name} is not available in this session")
        if name in WRITE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not isinstance(target, str) or not self._inside_workspace(target):
                return self._deny("Write only inside your workspace")
        if name == "Bash" and tool_input.get("dangerouslyDisableSandbox"):
            return self._deny("Commands always run inside the sandbox")
        return {}

    def _inside_workspace(self, target: str) -> bool:
        assert self.workspace is not None
        path = Path(target)
        if not path.is_absolute():
            path = self.workspace / path
        resolved = Path(os.path.realpath(path))
        return resolved.is_relative_to(Path(os.path.realpath(self.workspace)))

    @staticmethod
    def _deny(reason: str) -> dict[str, Any]:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    # -- turns --------------------------------------------------------------------------

    async def _turn_start(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.session is None or self.client is None or params.get("threadId") != self.session["id"]:
            raise BridgeError("Open the session before starting its turn")
        if self.turn is not None:
            raise BridgeError("One turn is permitted per activation")
        parts = params.get("input") or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text")
        assert self.record is not None
        turn = Turn(str(uuid4()), self.record.get("usage") or usage_totals(None))
        # Recorded before the prompt exists, so the session record always names the running turn.
        self.record["turns"].append({"id": turn.id, "prompt_sha256": prompt_digest(text), "status": "inProgress",
                                     "started_at": int(time.time() * 1000)})
        write_session(self.home, self.record)
        self.turn = turn
        message = {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
                   "session_id": self.session["id"], "uuid": turn.id}

        async def prompt():
            yield message
        await self.client.query(prompt())
        turn.task = asyncio.create_task(self._consume(turn), name="claude-turn")
        return {"turn": {"id": turn.id, "status": "inProgress"}}

    async def _interrupt(self, params: dict[str, Any], reason: str) -> dict[str, Any]:
        turn = self.turn
        if turn is None or (self.session and params.get("threadId", self.session["id"]) != self.session["id"]):
            return {}
        if turn.interrupt_reason is None:
            turn.interrupt_reason = reason
            await self.client.interrupt()
        return {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.peer is not None and self.session is not None
        await self.peer.notify(method, {"threadId": self.session["id"], **params})

    async def _turn_error(self, turn: Turn, code: str | None, message: str) -> None:
        """Report an error that ends the turn; the client never retries it."""
        error = {"code": code, "message": message} if code else {"message": message}
        await self._notify("error", {"turnId": turn.id, "willRetry": False, "error": error})

    async def _usage(self, turn: Turn, final: dict[str, int] | None = None) -> None:
        spent = usage_totals(None)
        for counts in turn.messages.values():
            spent = _add(spent, counts)
        if final is not None and final["totalTokens"] > spent["totalTokens"]:
            spent = final
        if spent["totalTokens"] <= turn.reported:
            return
        turn.reported = spent["totalTokens"]
        total = _add(turn.base, spent)
        # As in Codex, `last` is the latest request's own counts: its input is the context now.
        last = list(turn.messages.values())[-1] if turn.messages else spent
        assert self.record is not None
        # Durable before it is reported: a later activation never reports a lower total.
        self.record["usage"] = total
        write_session(self.home, self.record)
        await self._notify("thread/tokenUsage/updated", {"turnId": turn.id,
                                                         "tokenUsage": {"total": total, "last": last}})

    async def _consume(self, turn: Turn) -> None:
        sdk = self.sdk
        status, error = "failed", None
        try:
            await self._notify("turn/started", {"turn": {"id": turn.id, "status": "inProgress"}})
            async for message in self.client.receive_response():
                if isinstance(message, sdk.SystemMessage):
                    await self._system(turn, message)
                elif isinstance(message, sdk.AssistantMessage):
                    await self._assistant(turn, message)
                elif isinstance(message, sdk.UserMessage):
                    await self._results(turn, message)
                elif isinstance(message, sdk.RateLimitEvent):
                    await self._rate_limit(turn, message)
                elif isinstance(message, sdk.ResultMessage):
                    status, error = await self._result(turn, message)
                    if turn.interrupt_reason is not None and message.subtype == "error_during_execution":
                        # How Claude Code ends a turn it was asked to interrupt: the client asked
                        # for it, or the bridge reported its cause when it interrupted.
                        error = None
                    break
            else:
                error = {"message": "Claude Code ended the turn without a result"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            tail = " | ".join(list(self.stderr)[-5:])
            error = {"message": "Claude Code stopped during the turn: " + (str(exc) or type(exc).__name__)
                     + (" | " + tail if tail else "")}
        if turn.interrupt_reason is not None:
            status = "interrupted"
        assert self.record is not None
        for entry in self.record["turns"]:
            if entry["id"] == turn.id:
                entry.update(status=status, ended_at=int(time.time() * 1000))
        write_session(self.home, self.record)
        await self._notify("turn/completed", {"turn": {"id": turn.id, "status": status, "error": error}})

    async def _system(self, turn: Turn, message: Any) -> None:
        data = message.data if isinstance(message.data, dict) else {}
        if message.subtype == "init":
            if data.get("apiKeySource") not in {None, "none"}:
                await self._turn_error(turn, "claudeSubscriptionRequired",
                                       "Claude Code selected an API key; API billing is disabled")
                await self._interrupt({}, "billing")
            expected = self.params.get("cliVersion")
            actual = data.get("claude_code_version")
            if expected and actual and actual != expected:
                await self._turn_error(turn, "claudeVersionMismatch",
                                       f"Claude Code {actual} does not match the audited {expected}")
                await self._interrupt({}, "version")
        elif message.subtype == "compact_boundary":
            await self._notify("thread/compacted", {"turnId": turn.id})

    async def _assistant(self, turn: Turn, message: Any) -> None:
        if message.parent_tool_use_id:
            return  # Subagents are disabled; never attribute their text to this turn.
        if getattr(message, "error", None):
            text = "".join(getattr(block, "text", "") for block in message.content if hasattr(block, "text"))
            await self._turn_error(turn, ACCOUNT_ERRORS.get(message.error),
                                   text or f"Claude Code reported {message.error}")
        if message.usage:
            key = message.message_id or message.uuid or str(len(turn.messages))
            counts = usage_totals(message.usage)
            previous = turn.messages.get(key, {})
            turn.messages[key] = {name: max(value, previous.get(name, 0)) for name, value in counts.items()}
            await self._usage(turn)
        texts = [block.text for block in message.content if isinstance(block, self.sdk.TextBlock)]
        if texts and not getattr(message, "error", None):
            await self._notify("item/completed", {"turnId": turn.id, "item": {
                "type": "agentMessage", "id": message.uuid or "message-" + uuid4().hex, "text": "".join(texts)}})
        for block in message.content:
            if isinstance(block, self.sdk.ToolUseBlock) and not block.name.startswith(f"mcp__{SERVER}__"):
                item = self._action(block)
                turn.items[block.id] = item
                await self._notify("item/started", {"turnId": turn.id, "item": {**item, "status": "inProgress"}})

    def _action(self, block: Any) -> dict[str, Any]:
        tool_input = block.input if isinstance(block.input, dict) else {}
        if block.name == "Bash":
            return {"type": "commandExecution", "id": block.id, "command": str(tool_input.get("command", ""))[:4000],
                    "cwd": str(self.workspace)}
        if block.name in WRITE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            return {"type": "fileChange", "id": block.id,
                    "changes": [{"path": path}] if isinstance(path, str) else []}
        return {"type": "mcpToolCall", "id": block.id, "server": "claude-code", "tool": block.name}

    async def _results(self, turn: Turn, message: Any) -> None:
        if not isinstance(message.content, list):
            return
        for block in message.content:
            if isinstance(block, self.sdk.ToolResultBlock) and block.tool_use_id in turn.items:
                item = turn.items.pop(block.tool_use_id)
                await self._notify("item/completed", {"turnId": turn.id, "item": {
                    **item, "status": "failed" if block.is_error else "completed"}})

    async def _rate_limit(self, turn: Turn, event: Any) -> None:
        info = event.rate_limit_info
        raw = info.raw if isinstance(getattr(info, "raw", None), dict) else {}
        # Each change of a usage window's state (within, approaching, at its limit), for the Claude tab.
        await self._notify("account/rateLimit", {
            "status": info.status, "rateLimitType": getattr(info, "rate_limit_type", None),
            "utilization": getattr(info, "utilization", None), "resetsAt": info.resets_at})
        overage = info.rate_limit_type == "overage" or raw.get("errorCode") == "credits_required"
        if info.status == "rejected" or overage:
            resets = info.resets_at or info.overage_resets_at
            # Which limit (five_hour, seven_day, ...) and when it resets, recorded for the profile.
            kind = getattr(info, "rate_limit_type", None)
            await self._turn_error(turn, "claudeUsageLimit", "Claude subscription limit reached"
                                   + (f" ({kind})" if isinstance(kind, str) and kind else "")
                                   + (f"; resets at {resets}" if resets else "")
                                   + ("; usage credits are never used" if overage else ""))
            if turn.interrupt_reason is None:
                await self._interrupt({}, "usage_limit")

    async def _result(self, turn: Turn, result: Any) -> tuple[str, dict[str, Any] | None]:
        final = usage_totals(result.usage)
        if isinstance(result.model_usage, dict) and result.model_usage:
            modeled = usage_totals(None)
            for counts in result.model_usage.values():
                modeled = _add(modeled, usage_totals(counts))
            if modeled["totalTokens"] > final["totalTokens"]:
                final = modeled
        await self._usage(turn, final)
        if result.subtype == "success" and not result.is_error:
            return "completed", None
        detail = "; ".join(result.errors or []) or result.result or result.subtype
        error: dict[str, Any] = {"message": f"Claude Code ended the turn: {detail}"[:4000]}
        if result.api_error_status:
            error["status"] = result.api_error_status
        return "failed", error

    async def close(self) -> None:
        turn = self.turn
        if turn is not None and turn.task is not None and not turn.task.done():
            turn.task.cancel()
            await asyncio.gather(turn.task, return_exceptions=True)
        self._closing.set()
        if self._lifecycle is not None:
            await asyncio.gather(self._lifecycle, return_exceptions=True)


def load_sdk() -> Any:
    import claude_agent_sdk as sdk
    try:
        from claude_agent_sdk import _cli_version
        sdk._cli_version = _cli_version
    except ImportError:
        pass
    return sdk


async def serve(sdk: Any) -> None:
    # The SDK starts Claude Code with this process's environment; no API credential may reach it.
    for key in set(os.environ) - set(subscription_environment(dict(os.environ))):
        del os.environ[key]
    bridge = Bridge(sdk)
    bridge.peer = Peer(bridge.handle, max_frame_bytes=int(os.environ.get("FORMAL_GEYSER_BRIDGE_FRAME_BYTES", "67108864")))
    try:
        await bridge.peer.serve()
    finally:
        await bridge.close()


def main() -> int:
    asyncio.run(serve(load_sdk()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
