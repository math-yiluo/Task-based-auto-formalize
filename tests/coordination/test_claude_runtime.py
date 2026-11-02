"""Claude backend: the bridge, its subscription-only policy, and the shared harness contract.

Nothing here starts a model or Claude Code: the bridge runs against fake_claude_sdk.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from formal_geyser.coordination.models import ClaudeConfig
from formal_geyser.coordination.runtime import claude_bridge
from formal_geyser.coordination.runtime.claude import ClaudeHarness
from formal_geyser.coordination.runtime.claude_bridge import (
    Bridge,
    BridgeError,
    read_session,
    settings_for,
    subscription_environment,
    usage_totals,
)
from formal_geyser.coordination.runtime.rpc import RpcRemoteError
from formal_geyser.coordination.runtime.tools import dynamic_tools

from . import fake_claude_sdk as fake

LAUNCHER = """
import sys
sys.path.insert(0, {tests!r})
import fake_claude_sdk
fake_claude_sdk.select(sys.argv[1], None if sys.argv[2] == "-" else __import__("json").loads(sys.argv[2]))
sys.modules["claude_agent_sdk"] = fake_claude_sdk
from formal_geyser.coordination.runtime import claude_bridge
sys.exit(claude_bridge.main())
"""


def test_subscription_environment_removes_every_api_billing_source():
    environment = {"ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u",
                   "anthropic_profile": "p", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
                   "AWS_BEARER_TOKEN_BEDROCK": "b", "CLAUDECODE": "1",
                   "CLAUDE_CODE_OAUTH_TOKEN": "subscription", "PATH": "/bin"}
    assert subscription_environment(environment) == {"CLAUDE_CODE_OAUTH_TOKEN": "subscription", "PATH": "/bin"}


def test_usage_counts_cache_reads_as_input_in_either_spelling():
    snake = usage_totals({"input_tokens": 3, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7,
                          "output_tokens": 11})
    camel = usage_totals({"inputTokens": 3, "cacheReadInputTokens": 5, "cacheCreationInputTokens": 7,
                          "outputTokens": 11})
    assert snake == camel == {"totalTokens": 26, "inputTokens": 15, "cachedInputTokens": 5,
                              "cacheCreationInputTokens": 7, "outputTokens": 11}


def test_settings_confine_writes_and_never_reach_local_services(tmp_path):
    workspace, home = tmp_path / "work", tmp_path / "home"
    worker = settings_for(workspace=workspace, home=home, allowed_domains=["*"])
    assert "Bash" in worker["permissions"]["allow"]
    assert f"Edit(//{workspace.as_posix().lstrip('/')}/**)" in worker["permissions"]["allow"]
    assert "WebFetch(domain:*)" in worker["permissions"]["allow"]
    assert worker["permissions"]["defaultMode"] == "dontAsk"
    assert f"Read(//{(home / '.credentials.json').as_posix().lstrip('/')})" in worker["permissions"]["deny"]
    sandbox = worker["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"] and not sandbox["allowUnsandboxedCommands"]
    assert {"localhost", "127.0.0.1", "[::1]"} <= set(sandbox["network"]["deniedDomains"])
    named = settings_for(workspace=workspace, home=home, allowed_domains=["arxiv.org"])
    assert named["sandbox"]["network"]["allowedDomains"] == ["arxiv.org"]
    assert "WebFetch(domain:*)" not in named["permissions"]["allow"]


@pytest.mark.parametrize("release,interop,missing,expected", [
    ("4.4.0-19041-Microsoft", False, (), "WSL 2"),
    ("6.18.33.2-microsoft-standard-WSL2", True, (), "interop"),
    ("6.18.33.2-microsoft-standard-WSL2", False, ("socat",), "Install socat"),
    ("6.12.0-generic", False, ("bwrap", "socat"), "Install bwrap and socat"),
])
def test_claude_needs_a_full_sandbox(monkeypatch, release, interop, missing, expected):
    from formal_geyser.coordination import native_permissions
    from formal_geyser.coordination.runtime import claude

    monkeypatch.setattr(claude.platform, "release", lambda: release)
    monkeypatch.setattr(native_permissions, "wsl_interop", lambda: interop)
    monkeypatch.setattr(claude.shutil, "which", lambda name: None if name in missing else "/usr/bin/" + name)
    assert expected in claude.claude_unavailable(ClaudeConfig(home="/claude-home"))


def test_settings_keep_the_protected_paths_from_every_read(tmp_path):
    runtime, sign_in = tmp_path / "runtime", tmp_path / "codex" / "auth.json"
    runtime.mkdir()
    sign_in.parent.mkdir()
    sign_in.write_text("{}")
    worker = settings_for(workspace=tmp_path / "work", home=tmp_path / "home", allowed_domains=["*"],
                          protected=[str(runtime), str(sign_in)])
    assert set(worker["sandbox"]["filesystem"]["denyRead"]) == {
        str(tmp_path / "home" / ".credentials.json"), str(runtime), str(sign_in)}
    assert f"Read(//{runtime.as_posix().lstrip('/')}/**)" in worker["permissions"]["deny"]
    assert f"Read(//{sign_in.as_posix().lstrip('/')})" in worker["permissions"]["deny"]


class FakePeer:
    def __init__(self, reply=None):
        self.notifications: list[tuple[str, dict]] = []
        self.calls: list[dict] = []
        self.reply = reply or {"contentItems": [{"type": "inputText", "text": '{"ok":true}'}], "success": True}

    async def notify(self, method, params):
        self.notifications.append((method, params))

    async def request(self, method, params):
        assert method == "item/tool/call"
        self.calls.append(params)
        return self.reply

    def methods(self):
        return [method for method, _ in self.notifications]


async def _bridge(tmp_path, monkeypatch, scenario="complete", account=None, **params):
    fake.select(scenario, account)
    home = tmp_path / "claude-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    bridge = Bridge(fake)
    bridge.peer = FakePeer()
    assert bridge.handle is not None
    opened = await bridge.handle("thread/start", {
        "cwd": str(tmp_path / "work"), "instructions": "Work carefully.", "tools": dynamic_tools(),
        "allowedDomains": ["*"], "cliVersion": "2.1.286", **params})
    return bridge, home, opened


async def _turn(bridge, text="Do the task."):
    started = await bridge.handle("turn/start", {"threadId": bridge.session["id"],
                                                 "input": [{"type": "text", "text": text}]})
    await asyncio.wait_for(bridge.turn.task, 10)
    return started


async def test_a_turn_relays_tools_usage_and_compaction_in_the_harness_vocabulary(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    bridge, home, opened = await _bridge(tmp_path, monkeypatch)
    session_id = opened["thread"]["id"]
    client = fake.STATE["clients"][-1]
    options = client.options
    assert options.session_id == session_id and options.permission_mode == "dontAsk"
    assert options.setting_sources == [] and options.strict_mcp_config and options.verbatim_prompts
    assert options.env["CLAUDE_CODE_DISABLE_FAST_MODE"] == "1"
    assert options.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert set(options.mcp_servers["taskhub"]["instance"]) == {"taskhub_actions", "taskhub_call"}
    started = await _turn(bridge)
    turn_id = started["turn"]["id"]
    assert client.prompts[-1]["uuid"] == turn_id and client.prompts[-1]["message"]["content"] == "Do the task."
    call = bridge.peer.calls[0]
    assert call["turnId"] == turn_id and call["tool"] == "taskhub_actions" and call["callId"].startswith("toolu_")
    methods = bridge.peer.methods()
    assert methods[0] == "turn/started" and methods[-1] == "turn/completed"
    assert "thread/compacted" in methods
    actions = [params["item"] for method, params in bridge.peer.notifications
               if method in {"item/started", "item/completed"} and params["item"]["type"] == "commandExecution"]
    assert [item["status"] for item in actions] == ["inProgress", "completed"]
    usage = [params["tokenUsage"] for method, params in bridge.peer.notifications
             if method == "thread/tokenUsage/updated"]
    totals = [sample["total"]["totalTokens"] for sample in usage]
    # The result's total (380) exceeds the per-message sum (370), so it is the final sample.
    assert totals == [160, 370, 380]
    # Each sample's last is the latest request's own counts, as in Codex: its input is the
    # context, which the aggressive rotation and the context metrics read.
    assert [sample["last"]["inputTokens"] for sample in usage] == [150, 180, 180]
    terminal = bridge.peer.notifications[-1][1]["turn"]
    assert terminal == {"id": turn_id, "status": "completed", "error": None}
    record = read_session(home, session_id)
    assert record["usage"]["totalTokens"] == 380
    assert record["turns"][-1]["id"] == turn_id and record["turns"][-1]["status"] == "completed"
    assert [tool["name"] for tool in record["tools"]] == ["taskhub_actions", "taskhub_call"]
    await bridge.close()
    assert client.disconnected


async def test_no_prompt_is_sent_without_a_subscription_sign_in(tmp_path, monkeypatch):
    with pytest.raises(BridgeError) as missing:
        await _bridge(tmp_path, monkeypatch, account={})
    assert missing.value.code == "claudeSignInRequired"
    with pytest.raises(BridgeError) as keyed:
        await _bridge(tmp_path, monkeypatch, account={"subscriptionType": "max", "apiKeySource": "ANTHROPIC_API_KEY"})
    assert keyed.value.code == "claudeSubscriptionRequired"
    assert all(not client.prompts for client in fake.STATE["clients"])


async def test_a_usage_window_change_is_reported_and_the_turn_goes_on(tmp_path, monkeypatch):
    bridge, _, _ = await _bridge(tmp_path, monkeypatch, scenario="warning")
    await _turn(bridge)
    reports = [params for method, params in bridge.peer.notifications if method == "account/rateLimit"]
    assert [(report["status"], report["rateLimitType"], report["utilization"], report["resetsAt"])
            for report in reports] == [("allowed_warning", "five_hour", 0.8, 1790000000)]
    assert not [method for method, _ in bridge.peer.notifications if method == "error"]
    assert bridge.peer.notifications[-1][1]["turn"]["status"] == "completed"
    await bridge.close()


async def test_an_api_key_at_turn_start_or_paid_overage_stops_the_turn(tmp_path, monkeypatch):
    for scenario in ("api_key", "overage"):
        bridge, _, _ = await _bridge(tmp_path, monkeypatch, scenario=scenario)
        await _turn(bridge)
        errors = [params["error"]["code"] for method, params in bridge.peer.notifications if method == "error"]
        assert errors == ["claudeSubscriptionRequired" if scenario == "api_key" else "claudeUsageLimit"]
        assert fake.STATE["clients"][-1].interrupted.is_set()
        # The cause was reported once; the interrupted turn's own result adds no second error.
        assert bridge.peer.notifications[-1][1]["turn"] == {
            "id": bridge.peer.notifications[-1][1]["turn"]["id"], "status": "interrupted", "error": None}
        await bridge.close()


async def test_the_hook_confines_writes_and_refuses_unsandboxed_commands(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    bridge, _, _ = await _bridge(tmp_path, monkeypatch)
    client = fake.STATE["clients"][-1]
    allowed = await client.pre_tool_use("Write", {"file_path": str(work / "Main.lean")})
    outside = await client.pre_tool_use("Edit", {"file_path": str(tmp_path / "library" / "A.lean")})
    escape = await client.pre_tool_use("Write", {"file_path": str(work / ".." / "state.sqlite3")})
    unsandboxed = await client.pre_tool_use("Bash", {"command": "ls", "dangerouslyDisableSandbox": True})
    subagent = await client.pre_tool_use("Agent", {"prompt": "help"})
    assert allowed == {}
    for decision in (outside, escape, unsandboxed, subagent):
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    await bridge.close()


async def test_claude_is_shown_a_lenient_taskhub_call_and_the_runner_gets_the_exact_shape(tmp_path, monkeypatch):
    bridge, home, opened = await _bridge(tmp_path, monkeypatch)
    client = fake.STATE["clients"][-1]
    shown = {name: tool.input_schema for name, tool in client.options.mcp_servers["taskhub"]["instance"].items()}
    exact = {tool["name"]: tool["inputSchema"] for tool in dynamic_tools()}
    assert shown["taskhub_actions"] == exact["taskhub_actions"]
    lenient = shown["taskhub_call"]
    assert lenient["required"] == ["operation"] and lenient["additionalProperties"] is True
    assert lenient["properties"]["arguments"]["type"] == ["object", "string"]
    assert lenient["properties"]["operation"] == exact["taskhub_call"]["properties"]["operation"]
    # The session record, which the runner's catalog check compares with its own tools, keeps the
    # exact definitions: no stored catalog changes, and the gateway's own schema is untouched.
    assert read_session(home, opened["thread"]["id"])["tools"] == dynamic_tools()
    assert exact["taskhub_call"]["required"] == ["operation", "arguments"]
    assert exact["taskhub_call"]["additionalProperties"] is False

    bridge.turn = claude_bridge.Turn("turn-1", {})
    calls = bridge.peer.calls
    text = {"recipient_id": "Worker 1", "text": "Hello"}
    # Claude wrote the operation's fields beside operation, or arguments as JSON text (88 refusals a day).
    for written in ({"operation": "talk_send", **text}, {"operation": "talk_send", "arguments": json.dumps(text)},
                    {"operation": "talk_send", "arguments": {"text": "Hello"}, "recipient_id": "Worker 1"},
                    {"operation": "talk_send", "arguments": text}):
        reply = await client.call_tool("taskhub_call", written)
        assert reply["is_error"] is False
        assert calls[-1]["arguments"] == {"operation": "talk_send", "arguments": text}
        assert calls[-1]["callId"].startswith("toolu_")  # Still the call Claude Code made.
    await client.call_tool("taskhub_call", {"operation": "sync"})
    assert calls[-1]["arguments"] == {"operation": "sync", "arguments": {}}
    sent = len(calls)
    for written, detail in (({"operation": "talk_send", "arguments": "[1, 2]"}, "not as text"),
                            ({"operation": "talk_send", "arguments": "{not json"}, "not as text"),
                            ({"operation": "talk_send", "arguments": {"text": "Hello"}, "text": "Other"},
                             "give each field once, inside arguments: text")):
        reply = await client.call_tool("taskhub_call", written)
        body = json.loads(reply["content"][0]["text"])
        assert reply["is_error"] is True and body["code"] == "INVALID_ARGUMENTS" and detail in body["error"]
    assert len(calls) == sent
    await bridge.close()


async def test_resume_reopens_the_recorded_session_and_a_fork_is_refused(tmp_path, monkeypatch):
    bridge, home, opened = await _bridge(tmp_path, monkeypatch)
    source = opened["thread"]["id"]
    await _turn(bridge)
    first = read_session(home, source)["turns"][0]["id"]
    await bridge.close()
    # Claude Code saves a session's transcript once a turn has run.
    transcript = home / "projects" / claude_bridge.PROJECT_DIR_NAME / (source + ".jsonl")
    transcript.parent.mkdir(parents=True)
    lines = [{"uuid": first, "type": "user", "parentUuid": None, "message": {"content": "Do the task."}},
             {"uuid": "end-1", "type": "assistant", "parentUuid": first}]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    resumed = Bridge(fake)
    resumed.peer = FakePeer()
    reply = await resumed.handle("thread/resume", {"cwd": str(tmp_path / "work"), "threadId": source,
                                                   "instructions": "", "allowedDomains": []})
    options = fake.STATE["clients"][-1].options
    # The session itself is reopened, not a copy of it.
    assert reply["thread"]["id"] == source and options.resume == source
    assert not getattr(options, "fork_session", False)
    await resumed.close()

    # Sessions are never forked: the request is refused before Claude Code starts or a record is written.
    sessions = sorted((home / "task-based" / "sessions").iterdir())
    clients = len(fake.STATE["clients"])
    with pytest.raises(BridgeError, match="Unsupported request: thread/fork"):
        await Bridge(fake).handle("thread/fork", {"cwd": str(tmp_path / "helper"), "threadId": source,
                                                  "lastTurnId": first, "instructions": ""})
    assert len(fake.STATE["clients"]) == clients
    assert sorted((home / "task-based" / "sessions").iterdir()) == sessions
    with pytest.raises(BridgeError):
        await Bridge(fake).handle("thread/resume", {"cwd": str(tmp_path), "threadId": "not-a-uuid"})


def _harness(tmp_path, scenario, account=None):
    launcher = tmp_path / "launch_bridge.py"
    launcher.write_text(LAUNCHER.format(tests=str(Path(__file__).parent)), encoding="utf-8")
    config = ClaudeConfig(home=str(tmp_path / "claude-home"), stop_grace_seconds=5)
    command = [sys.executable, "-u", str(launcher), scenario, "-" if account is None else json.dumps(account)]
    return ClaudeHarness(config, command=command)


async def test_harness_runs_a_turn_through_the_bridge_and_closes_quietly(tmp_path):
    harness = _harness(tmp_path, "complete")
    session = await harness.open(workspace=tmp_path / "work", thread_id=None,
                                 tools=dynamic_tools(), instructions="Work carefully.")
    started, calls, samples = [], [], []

    async def on_tool(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return {"actions": []}

    async def on_usage(sample):
        samples.append(sample)

    async def on_started(turn_id):
        started.append(turn_id)

    outcome = await harness.run_turn("Do the task.", on_tool=on_tool, on_usage=on_usage, on_started=on_started)
    assert outcome.status == "completed" and outcome.terminal_observed and outcome.error is None
    assert outcome.process_quiet and harness.process_quiet
    assert started == [outcome.turn_id] and calls[0][0] == "taskhub_actions"
    # The conversation's total, and the last request's own counts (the second message's 210).
    assert samples[-1]["totalTokens"] == 380 and samples[-1]["lastTokenUsage"]["totalTokens"] == 210
    assert "Checked:" in outcome.visible_output
    catalog = await ClaudeHarness(harness.config).stored_tool_catalog(session.thread_id, workspace=tmp_path)
    assert [tool["name"] for tool in catalog] == ["taskhub_actions", "taskhub_call"]


async def test_harness_drains_on_a_runtime_directive(tmp_path):
    harness = _harness(tmp_path, "drain")
    diagnostics = []

    async def diagnostic(record):
        diagnostics.append(record)
    harness.set_diagnostic_handler(diagnostic)
    await harness.open(workspace=tmp_path / "work", thread_id=None, tools=dynamic_tools(), instructions="")

    async def on_tool(name, arguments, call_id):
        return {"waiting": True, "runtime_directive": "drain_for_wait"}

    async def ignore(*_):
        return None

    outcome = await harness.run_turn("Wait.", on_tool=on_tool, on_usage=ignore, on_started=ignore)
    assert outcome.status == "interrupted" and outcome.directive == "drain_for_wait"
    # Claude Code ends the interrupted turn with error_during_execution: a drain, not an error.
    assert outcome.error is None and diagnostics == []
    assert outcome.process_quiet


async def test_harness_holds_claude_dispatch_when_the_subscription_is_missing(tmp_path):
    harness = _harness(tmp_path, "complete", account={})
    records = []

    async def control(record):
        records.append(record)
    harness.set_control_handler(control)
    with pytest.raises(RpcRemoteError):
        await harness.open(workspace=tmp_path / "work", thread_id=None, tools=dynamic_tools(), instructions="")
    assert [record["code"] for record in records] == ["claudeSignInRequired"]
    assert harness.process_quiet


async def test_harness_refuses_an_unaudited_sdk(tmp_path):
    harness = _harness(tmp_path, "complete")
    harness.config = harness.config.model_copy(update={"sdk_version": "0.0.1"})
    with pytest.raises(Exception, match="does not match the audited 0.0.1"):
        await harness.open(workspace=tmp_path / "work", thread_id=None, tools=dynamic_tools(), instructions="")
    assert harness.process_quiet


def test_steering_is_not_offered():
    assert not callable(getattr(ClaudeHarness(ClaudeConfig(home="/tmp/claude")), "steer", None))


async def test_a_claude_session_is_always_a_workers_own_session(tmp_path):
    # Decisions run on Codex; a read-only or answer-schema session never starts on Claude.
    for options in ({"read_only": True}, {}):
        harness = ClaudeHarness(ClaudeConfig(home=str(tmp_path / "claude")))
        if not options:
            harness.set_turn_options(output_schema={"type": "object"})
        with pytest.raises(RuntimeError, match="decisions run on Codex"):
            await harness.open(workspace=tmp_path / "work", thread_id=None, tools=[], instructions="", **options)
        assert harness.host is None
