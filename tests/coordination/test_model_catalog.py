from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

from formal_geyser.coordination.models import CodexConfig
from formal_geyser.coordination.runtime import model_catalog
from formal_geyser.coordination.runtime.codex import CodexHarness
from formal_geyser.coordination.runtime.model_catalog import (
    ModelCatalogError,
    discover_models,
)
from formal_geyser.coordination.runtime.process import (
    OwnedProcessHost,
    ProcessHostError,
)

FAKE_SERVER = """
import json,os,subprocess,sys,time
from pathlib import Path
plan=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
log=Path(sys.argv[2])
if plan.get('environment_file'):
    Path(plan['environment_file']).write_text(json.dumps({
        'cwd':os.getcwd(), 'CODEX_HOME':os.environ.get('CODEX_HOME'),
        'CODEX_SQLITE_HOME':os.environ.get('CODEX_SQLITE_HOME')}), encoding='utf-8')
if plan.get('descendant'):
    subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'])
if plan.get('large_stderr'):
    sys.stderr.write('synthetic stderr '*30000);sys.stderr.flush()
page=0
for line in sys.stdin:
    msg=json.loads(line)
    with log.open('a', encoding='utf-8') as output:
        output.write(json.dumps(msg)+'\\n')
    method=msg.get('method')
    if method==plan.get('hang_method'):
        Path(plan['hang_marker']).write_text('waiting', encoding='utf-8')
        time.sleep(120)
    if method==plan.get('fail_method'):
        print(json.dumps({'id':msg['id'],'error':plan['error']}),flush=True)
        continue
    if method==plan.get('eof_method'):
        break
    if method=='initialize':
        result=plan['initialize']
    elif method=='initialized':
        continue
    elif method=='account/read':
        result=plan['account']
    elif method=='model/list':
        time.sleep(plan.get('page_delay',0))
        result=plan['pages'][page];page+=1
    else:
        raise RuntimeError('Unexpected client request: '+str(method))
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
"""


def model_record(**changes):
    return {
        "id": "picker-record-1",
        "model": "provider/model-one",
        "displayName": "First available model",
        "description": "Synthetic model metadata.",
        "hidden": False,
        "defaultReasoningEffort": "consider",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "consider", "description": "A synthetic effort."},
            {"reasoningEffort": "consider-further", "description": "Another synthetic effort."},
        ],
        "isDefault": True,
        **changes,
    }


def fake_server(tmp_path: Path, **changes):
    script = tmp_path / "catalog_peer.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    plan = {
        "initialize": {"userAgent": "Codex Desktop/0.160.0 (synthetic peer)"},
        "account": {"account": {"type": "synthetic-auth"}, "requiresOpenaiAuth": True},
        "pages": [{"data": [model_record()], "nextCursor": None}],
        **changes,
    }
    plan_path = tmp_path / "catalog_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    log = tmp_path / "requests.jsonl"
    return [sys.executable, "-u", str(script), str(plan_path), str(log)], log


def config(**changes):
    return CodexConfig(version="0.160.0", stop_grace_seconds=0.2, **changes)


def requests(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
async def transports(monkeypatch):
    hosts, clients = [], []
    original_start = OwnedProcessHost.start
    original_stop = OwnedProcessHost.stop
    original_client = model_catalog.JsonRpcClient

    async def start(*args, **kwargs):
        host = await original_start(*args, **kwargs)
        hosts.append(host)
        return host

    def client(*args, **kwargs):
        rpc = original_client(*args, **kwargs)
        clients.append(rpc)
        return rpc

    monkeypatch.setattr(OwnedProcessHost, "start", start)
    monkeypatch.setattr(model_catalog, "JsonRpcClient", client)
    yield hosts, clients
    try:
        for host in hosts:
            assert host.tree_quiet
            assert host.process.returncode is not None
        for rpc in clients:
            assert rpc._closed
            assert all(task.done() for task in rpc._tasks)
    finally:
        for host in hosts:
            await original_stop(host, grace=0.1)
        for rpc in clients:
            await rpc.close()


async def test_reads_all_pages_and_deduplicates_actual_model_and_effort_values(tmp_path, transports):
    first = model_record()
    first["supportedReasoningEfforts"].append(copy.deepcopy(first["supportedReasoningEfforts"][0]))
    repeated = model_record(id="different-picker-id")
    second = model_record(id="picker-record-2", model="another-model", displayName="Second model",
                          isDefault=False, defaultReasoningEffort="consider-further")
    second.pop("description")
    hidden = model_record(model="hidden-model", hidden=True)
    command, log = fake_server(tmp_path, pages=[
        {"data": [first, hidden], "nextCursor": "opaque-page-2"},
        {"data": [repeated, second], "nextCursor": "opaque-page-3"},
        {"data": [], "nextCursor": None},
    ], large_stderr=True, descendant=True)
    result = await discover_models(config(), tmp_path, command=command)
    assert result == {
        "models": [
            {"id": "provider/model-one", "name": "First available model",
             "description": "Synthetic model metadata.", "default_effort": "consider",
             "efforts": [{"id": item["reasoningEffort"], "description": item["description"]}
                         for item in repeated["supportedReasoningEfforts"]], "is_default": True},
            {"id": "another-model", "name": "Second model", "description": "",
             "default_effort": "consider-further",
             "efforts": [{"id": item["reasoningEffort"], "description": item["description"]}
                         for item in second["supportedReasoningEfforts"]], "is_default": False},
        ],
        "authentication_required": False,
        "source": "codex/model/list",
    }
    messages = requests(log)
    assert [message["method"] for message in messages] == [
        "initialize", "initialized", "account/read", "model/list", "model/list", "model/list"]
    assert messages[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert messages[2]["params"] == {"refreshToken": False}
    assert [message["params"] for message in messages[3:]] == [
        {"limit": 20, "includeHidden": False},
        {"limit": 20, "includeHidden": False, "cursor": "opaque-page-2"},
        {"limit": 20, "includeHidden": False, "cursor": "opaque-page-3"},
    ]


@pytest.mark.parametrize(("account", "requires_auth", "expected"), [
    (None, True, True),
    (None, False, False),
    ({"type": "chatgpt", "email": "synthetic@example.invalid", "token": "synthetic-secret"}, True, False),
    ({"type": "other-provider"}, False, False),
])
async def test_authentication_status_exposes_no_account_fields(
    tmp_path, transports, account, requires_auth, expected,
):
    command, _ = fake_server(tmp_path, account={"account": account, "requiresOpenaiAuth": requires_auth})
    result = await discover_models(config(), tmp_path, command=command)
    assert result["authentication_required"] is expected
    assert "synthetic@example.invalid" not in json.dumps(result)
    assert "synthetic-secret" not in json.dumps(result)
    assert set(result) == {"models", "authentication_required", "source"}


async def test_uses_harness_launch_command_and_profile_environment(tmp_path, monkeypatch, transports):
    environment_path = tmp_path / "environment.json"
    command, _ = fake_server(tmp_path, environment_file=str(environment_path))
    seen = []

    def launch(cls, settings, protected=()):
        seen.append(settings)
        return command

    monkeypatch.setattr(CodexHarness, "launch_command", classmethod(launch))
    settings = config(home=str(tmp_path / "synthetic-profile"))
    await discover_models(settings, tmp_path)
    assert seen == [settings]
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    assert Path(environment["cwd"]) == tmp_path.resolve()
    assert environment["CODEX_HOME"] == environment["CODEX_SQLITE_HOME"] == settings.home


@pytest.mark.parametrize("initialized", [
    None, {}, {"userAgent": False}, {"userAgent": "Codex/0.160.01"},
    {"userAgent": "Codex/0.144.6"}, {"userAgent": "synthetic-account-secret"},
])
async def test_rejects_unaudited_or_missing_version_before_catalog_calls(tmp_path, transports, initialized):
    command, log = fake_server(tmp_path, initialize=initialized)
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command)
    assert failure.value.code == "model_catalog_version_mismatch"
    assert "synthetic-account-secret" not in str(failure.value)
    assert [message["method"] for message in requests(log)] == ["initialize"]


@pytest.mark.parametrize("changes", [
    {"model": None}, {"id": ""}, {"displayName": "  "}, {"description": 17},
    {"hidden": "false"}, {"isDefault": 1}, {"supportedReasoningEfforts": []},
    {"supportedReasoningEfforts": {}}, {"supportedReasoningEfforts": [None]},
    {"supportedReasoningEfforts": [{"reasoningEffort": "", "description": ""}]},
    {"supportedReasoningEfforts": [{"reasoningEffort": "consider", "description": None}]},
    {"defaultReasoningEffort": "unadvertised-effort"},
    {"supportedReasoningEfforts": [
        {"reasoningEffort": "consider", "description": "First definition"},
        {"reasoningEffort": "consider", "description": "Conflicting definition"},
    ]},
])
async def test_rejects_invalid_model_capabilities(tmp_path, transports, changes):
    command, _ = fake_server(tmp_path, pages=[{"data": [model_record(**changes)], "nextCursor": None}])
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command)
    assert failure.value.code == "model_catalog_invalid"


@pytest.mark.parametrize("pages", [
    [None], [{"data": {}}], [{"data": []}], [{"data": [None], "nextCursor": None}],
    [{"data": [], "nextCursor": ""}], [{"data": [], "nextCursor": 4}],
    [{"data": [], "nextCursor": "cycle"}, {"data": [], "nextCursor": "cycle"}],
    [{"data": [model_record()], "nextCursor": "second"},
     {"data": [model_record(displayName="Conflicting catalog update")], "nextCursor": None}],
    [{"data": [model_record(), model_record(id="other-id", model="other-model")], "nextCursor": None}],
])
async def test_rejects_invalid_pages_and_conflicting_catalogs(tmp_path, transports, pages):
    command, _ = fake_server(tmp_path, pages=pages)
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command)
    assert failure.value.code == "model_catalog_invalid"


@pytest.mark.parametrize("account", [
    {}, {"account": None}, {"account": None, "requiresOpenaiAuth": "true"},
    {"account": [], "requiresOpenaiAuth": True}, {"account": {}, "requiresOpenaiAuth": True},
])
async def test_rejects_ambiguous_authentication_status(tmp_path, transports, account):
    command, log = fake_server(tmp_path, account=account)
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command)
    assert failure.value.code == "model_catalog_invalid"
    assert "model/list" not in [message["method"] for message in requests(log)]


@pytest.mark.parametrize("method", ["initialize", "account/read", "model/list"])
async def test_remote_failure_is_sanitized_and_does_not_retry(tmp_path, transports, method):
    command, log = fake_server(tmp_path, fail_method=method, error={
        "code": -32000, "message": "synthetic-secret synthetic@example.invalid",
        "data": {"token": "synthetic-token"},
    })
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command)
    assert failure.value.code == "model_catalog_unavailable"
    assert "synthetic" not in json.dumps(failure.value.as_dict())
    assert failure.value.__suppress_context__
    assert sum(message["method"] == method for message in requests(log)) == 1


async def test_process_exit_during_request_is_controlled(tmp_path, transports):
    command, _ = fake_server(tmp_path, eof_method="model/list")
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command)
    assert failure.value.code == "model_catalog_unavailable"


async def test_missing_executable_is_controlled(tmp_path, transports):
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=[str(tmp_path / "missing-program")])
    assert failure.value.code == "model_catalog_unavailable"


@pytest.mark.parametrize("method", ["initialize", "account/read", "model/list"])
async def test_metadata_timeout_waits_for_owned_process_cleanup(tmp_path, transports, method):
    marker = tmp_path / "blocked"
    command, _ = fake_server(tmp_path, hang_method=method, hang_marker=str(marker), descendant=True)
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command, timeout=1.0)
    assert marker.exists()
    assert failure.value.code == "model_catalog_timeout"


async def test_timeout_bounds_the_whole_paginated_read(tmp_path, transports):
    command, log = fake_server(tmp_path, page_delay=0.35, pages=[
        {"data": [], "nextCursor": f"page-{index}"} for index in range(20)
    ])
    # Leave room for several pages even when process startup is slow under load.
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command, timeout=3.0)
    assert failure.value.code == "model_catalog_timeout"
    assert 1 < sum(message["method"] == "model/list" for message in requests(log)) < 20


async def test_repeated_cancellation_does_not_interrupt_cleanup(tmp_path, monkeypatch, transports):
    marker = tmp_path / "blocked"
    command, _ = fake_server(tmp_path, hang_method="model/list", hang_marker=str(marker), descendant=True)
    original_stop = OwnedProcessHost.stop
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def delayed_stop(host, *, grace):
        cleaning.set()
        await release.wait()
        return await original_stop(host, grace=grace)

    monkeypatch.setattr(OwnedProcessHost, "stop", delayed_stop)
    discovery = asyncio.create_task(discover_models(config(), tmp_path, command=command))
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        discovery.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        discovery.cancel()
        await asyncio.sleep(0)
        assert not discovery.done()
        assert transports[0] and not transports[0][0].tree_quiet
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await discovery
    finally:
        release.set()
        if not discovery.done():
            discovery.cancel()
        await asyncio.gather(discovery, return_exceptions=True)


async def test_cleanup_failure_is_explicit_and_still_closes_rpc(tmp_path, monkeypatch, transports):
    command, _ = fake_server(tmp_path)
    original_stop = OwnedProcessHost.stop

    async def failed_confirmation(host, *, grace):
        await original_stop(host, grace=grace)
        raise ProcessHostError("synthetic sensitive process detail")

    monkeypatch.setattr(OwnedProcessHost, "stop", failed_confirmation)
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command)
    assert failure.value.code == "model_catalog_cleanup_failed"
    assert "synthetic sensitive" not in str(failure.value)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan"), True])
async def test_timeout_must_be_finite_and_positive(tmp_path, transports, timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        await discover_models(config(), tmp_path, command=[sys.executable], timeout=timeout)
    assert not transports[0]


async def test_cancellation_during_startup_still_closes_owned_tree(tmp_path, monkeypatch):
    from formal_geyser.coordination.runtime import process as process_module

    command, _ = fake_server(tmp_path)
    original_stop = OwnedProcessHost.stop
    captured = []
    cleaning = asyncio.Event()

    async def stopped(host, *, grace):
        captured.append(host)
        cleaning.set()
        return await original_stop(host, grace=grace)

    # The server's first process is never seen in its cgroup, so its startup keeps waiting.
    monkeypatch.setattr(process_module.cgroups, "processes", lambda cgroup: [])
    monkeypatch.setattr(OwnedProcessHost, "stop", stopped)
    with pytest.raises(ModelCatalogError) as failure:
        await discover_models(config(), tmp_path, command=command, timeout=0.5)
    assert failure.value.code == "model_catalog_timeout"
    assert cleaning.is_set() and captured and all(host.tree_quiet for host in captured)
