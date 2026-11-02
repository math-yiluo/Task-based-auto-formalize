"""A model-free stand-in for claude_agent_sdk, driven by named scenarios.

Only the surface claude_bridge uses is modelled. A scenario receives the fake client and
the prompt message and yields SDK messages; it can call TaskHub tools through the real
PreToolUse hook and MCP handler the bridge registered.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

__version__ = "0.2.163"


class _cli_version:  # noqa: N801 - stands in for the claude_agent_sdk._cli_version module
    __cli_version__ = "2.1.286"


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool | None = None


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "claude-test"
    parent_tool_use_id: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    message_id: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    uuid: str | None = None


@dataclass
class UserMessage:
    content: Any
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    tool_use_result: dict[str, Any] | None = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 1
    session_id: str = ""
    stop_reason: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    result: str | None = None
    structured_output: Any = None
    model_usage: dict[str, Any] | None = None
    permission_denials: list[Any] | None = None
    deferred_tool_use: Any = None
    errors: list[str] | None = None
    api_error_status: int | None = None
    uuid: str | None = None
    terminal_reason: str | None = None


@dataclass
class RateLimitInfo:
    status: str
    resets_at: int | None = None
    rate_limit_type: str | None = None
    utilization: float | None = None
    overage_status: str | None = None
    overage_resets_at: int | None = None
    overage_disabled_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitEvent:
    rate_limit_info: RateLimitInfo
    uuid: str = ""
    session_id: str = ""


@dataclass
class HookMatcher:
    matcher: str | None = None
    hooks: list[Any] = field(default_factory=list)
    timeout: float | None = None


@dataclass
class SdkMcpTool:
    name: str
    description: str
    input_schema: Any
    handler: Any
    annotations: Any = None


class ToolAnnotations:
    def __init__(self, **values: Any):
        self.__dict__.update(values)


def create_sdk_mcp_server(name: str, version: str = "1.0.0", tools: list[SdkMcpTool] | None = None):
    return {"type": "sdk", "name": name, "instance": {tool.name: tool for tool in tools or []}}


class ClaudeAgentOptions:
    def __init__(self, **values: Any):
        self.__dict__.update(values)


SUBSCRIPTION = {"subscriptionType": "max", "apiKeySource": "none", "tokenSource": "claude.ai"}
STATE: dict[str, Any] = {"scenario": "complete", "account": dict(SUBSCRIPTION), "clients": []}


def select(name: str, account: dict[str, Any] | None = None) -> None:
    STATE.update(scenario=name, account=dict(SUBSCRIPTION) if account is None else account, clients=[])


class ClaudeSDKClient:
    def __init__(self, options: ClaudeAgentOptions):
        self.options = options
        self.prompts: list[dict[str, Any]] = []
        self.interrupted = asyncio.Event()
        self.disconnected = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._producer: asyncio.Task | None = None
        STATE["clients"].append(self)

    async def connect(self, prompt: Any = None) -> None:
        if STATE["scenario"] == "no_start":
            raise RuntimeError("Claude Code exited at startup")

    async def get_server_info(self) -> dict[str, Any]:
        return {"account": STATE["account"]}

    async def query(self, prompt: Any) -> None:
        async for message in prompt:
            self.prompts.append(message)
        scenario = SCENARIOS[STATE["scenario"]]

        async def produce():
            async for item in scenario(self, self.prompts[-1]):
                await self._queue.put(item)
        self._producer = asyncio.create_task(produce())

    async def receive_response(self):
        while True:
            message = await self._queue.get()
            yield message
            if isinstance(message, ResultMessage):
                return

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def disconnect(self) -> None:
        self.disconnected = True
        if self._producer is not None and not self._producer.done():
            self._producer.cancel()

    async def pre_tool_use(self, name: str, tool_input: dict[str, Any], tool_use_id: str | None = None):
        tool_use_id = tool_use_id or "toolu_" + uuid4().hex
        outcome: dict[str, Any] = {}
        for matcher in self.options.hooks.get("PreToolUse", []):
            for hook in matcher.hooks:
                outcome = await hook({"tool_name": name, "tool_input": tool_input, "tool_use_id": tool_use_id},
                                     tool_use_id, None) or outcome
        return outcome

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.pre_tool_use("mcp__taskhub__" + name, arguments)
        return await self.options.mcp_servers["taskhub"]["instance"][name].handler(arguments)


def _usage(input_tokens: int, output_tokens: int, cached: int = 0) -> dict[str, int]:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}


def _init(**data: Any) -> SystemMessage:
    return SystemMessage("init", {"apiKeySource": "none", "claude_code_version": "2.1.286",
                                  "model": "claude-test", **data})


async def complete(client: ClaudeSDKClient, prompt: dict[str, Any]):
    yield _init()
    reply = await client.call_tool("taskhub_actions", {})
    yield AssistantMessage([ToolUseBlock("toolu_bash", "Bash", {"command": "cat Main.lean"})],
                           usage=_usage(100, 10, 50), message_id="msg_1", uuid=str(uuid4()))
    yield UserMessage([ToolResultBlock("toolu_bash", "ok", False)])
    yield AssistantMessage([TextBlock("Checked: " + reply["content"][0]["text"][:40])],
                           usage=_usage(120, 30, 60), message_id="msg_2", uuid=str(uuid4()))
    yield SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "auto"}})
    yield ResultMessage(usage=_usage(220, 50, 110), result="done", uuid=str(uuid4()))


async def drain(client: ClaudeSDKClient, prompt: dict[str, Any]):
    yield _init()
    await client.call_tool("taskhub_call", {"operation": "wait", "arguments": {}})
    await asyncio.wait_for(client.interrupted.wait(), 10)
    yield ResultMessage(subtype="error_during_execution", is_error=True, usage=_usage(10, 1))


async def overage(client: ClaudeSDKClient, prompt: dict[str, Any]):
    yield _init()
    yield RateLimitEvent(RateLimitInfo("allowed", rate_limit_type="overage", resets_at=1790000000))
    await asyncio.wait_for(client.interrupted.wait(), 10)
    yield ResultMessage(subtype="error_during_execution", is_error=True)


async def warning(client: ClaudeSDKClient, prompt: dict[str, Any]):
    yield _init()
    yield RateLimitEvent(RateLimitInfo("allowed_warning", rate_limit_type="five_hour", utilization=0.8,
                                       resets_at=1790000000))
    yield ResultMessage(usage=_usage(10, 1), result="done", uuid=str(uuid4()))


async def api_key(client: ClaudeSDKClient, prompt: dict[str, Any]):
    yield _init(apiKeySource="ANTHROPIC_API_KEY")
    await asyncio.wait_for(client.interrupted.wait(), 10)
    yield ResultMessage(subtype="error_during_execution", is_error=True)


SCENARIOS = {"complete": complete, "drain": drain, "overage": overage, "warning": warning, "api_key": api_key,
             "no_start": complete}
