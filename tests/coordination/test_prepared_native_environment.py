from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from formal_geyser.coordination.models import ClaudeConfig, CodexConfig
from formal_geyser.coordination.runtime.claude import ClaudeHarness
from formal_geyser.coordination.runtime.codex import CodexHarness

# Lean runs only in the project's Lean service (lean_check): workers get none of these.
LEAN_VARIABLES = ("LEAN", "LAKE", "LEAN_PATH", "LEAN_SYSROOT", "LAKE_HOME")
LEAN_COMMANDS = ("lean", "lake", "elan")


def test_worker_python_reads_and_prints_utf8(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    harness = CodexHarness(CodexConfig(home=str(tmp_path / "home")), command=[sys.executable])
    assert harness.process_environment()["PYTHONUTF8"] == "1"


def prepared_environment(root: Path) -> dict:
    cache = root / ".dependencies" / "pinned"
    binary = cache / "lean" / "bin"
    binary.mkdir(parents=True)
    lean, lake = binary / "lean", binary / "lake"
    lean.write_text("synthetic executable identity", encoding="utf-8")
    lake.write_text("synthetic executable identity", encoding="utf-8")
    sources = [cache / "mathlib", cache / "mathlib/.lake/packages/batteries"]
    libraries = [source / ".lake/build/lib/lean" for source in sources]
    for library in libraries:
        library.mkdir(parents=True)
    return {"format": 1, "state": "ready", "environment_artifact": "approved-environment",
            "pins": {"lean_toolchain": "synthetic-pin"},
            "cache": {"lean_executable": str(lean), "lake_executable": str(lake),
                      "library_roots": [str(path) for path in libraries],
                      "source_roots": [str(path) for path in sources],
                      "cache_directory": str(cache), "receipt_path": str(cache / "ready.json")}}


def test_prepared_environment_gives_workers_no_lean_tools_without_mutating_parent(tmp_path, monkeypatch):
    prepared = prepared_environment(tmp_path)
    cache = prepared["cache"]
    # Inherited PATH directories holding Lean, Lake or elan executables, beside an ordinary one.
    toolchain = str(Path(cache["lean_executable"]).parent)
    elan = tmp_path / "elan" / "bin"
    elan.mkdir(parents=True)
    (elan / "elan").write_text("synthetic executable identity", encoding="utf-8")
    ordinary = tmp_path / "ordinary-tools"
    ordinary.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([toolchain, str(elan), str(ordinary), os.environ.get("PATH", "")]))
    monkeypatch.setenv("LEAN", cache["lean_executable"])
    monkeypatch.setenv("LEAN_PATH", "unrelated-library")
    monkeypatch.setenv("LEAN_SYSROOT", "unrelated-toolchain")
    monkeypatch.setenv("LEAN_GITHASH", "unrelated-hash")
    monkeypatch.setenv("LAKE_OVERRIDE_LEAN", "true")
    monkeypatch.setenv("ELAN_TOOLCHAIN", "unrelated-version")
    monkeypatch.setenv("ELAN_HOME", str(elan.parent))
    monkeypatch.setenv("NATIVE_TOOL_SETTING", "retained")
    before = dict(os.environ)
    harness = CodexHarness(CodexConfig(), command=[sys.executable], prepared_environment=prepared)
    environment = harness.process_environment()
    assert environment is not None
    path = environment["PATH"].split(os.pathsep)
    assert path[0] == str(Path(sys.executable).parent) and str(ordinary) in path
    assert toolchain not in path and str(elan) not in path
    assert not any((Path(entry) / name).is_file() for entry in path for name in LEAN_COMMANDS)
    assert environment["FORMAL_GEYSER_PYTHON"] == sys.executable
    assert not set(LEAN_VARIABLES).intersection(environment)
    assert environment["LEAN_SRC_PATH"].split(os.pathsep) == [".", *cache["source_roots"]]
    assert not {"LEAN_GITHASH", "LAKE_OVERRIDE_LEAN", "ELAN_TOOLCHAIN", "ELAN_HOME"}.intersection(environment)
    assert environment["NATIVE_TOOL_SETTING"] == "retained"
    assert os.environ == before
    # The harness freezes the verified receipt, rather than following a mutable caller dict.
    prepared["cache"]["source_roots"].append(str(tmp_path / "new-unverified-source"))
    assert harness.process_environment() == environment
    # A Claude worker's shell gets the same environment.
    claude = ClaudeHarness(ClaudeConfig(home=str(tmp_path / "claude")), prepared_environment=prepared)
    claude_environment = claude.process_environment()
    assert {key: value for key, value in claude_environment.items() if key != "CLAUDE_CONFIG_DIR"
            and not key.startswith("FORMAL_GEYSER_BRIDGE")}.keys() <= environment.keys()
    assert not {"LEAN_GITHASH", "LAKE_OVERRIDE_LEAN", "ELAN_TOOLCHAIN", "ELAN_HOME"}.intersection(claude_environment)


@pytest.mark.parametrize("field,value,reason", [
    ("source_roots", "not-a-list", "nonempty source_roots"), ("source_roots", [], "nonempty source_roots"),
    ("source_roots", ["relative/mathlib"], "must be absolute"),
    ("source_roots", [os.pathsep.join(os.path.abspath(os.sep + name) for name in ("mathlib", "batteries"))],
     "search-path separator"),
])
def test_malformed_prepared_paths_fail_before_process_start(tmp_path, field, value, reason):
    prepared = prepared_environment(tmp_path)
    prepared["cache"][field] = value
    harness = CodexHarness(CodexConfig(), command=[sys.executable], prepared_environment=prepared)
    with pytest.raises(ValueError, match="Prepared Lean environment .*" + reason):
        harness.process_environment()
    assert harness.host is None


@pytest.mark.parametrize("field,value", [
    ("lean_executable", "relative/lean"), ("library_roots", []), ("lake_executable", None),
])
def test_prepared_lean_tool_paths_never_reach_workers(tmp_path, field, value):
    """The workers' environment reads no prepared Lean or Lake executable or compiled library,
    so even a malformed one changes nothing."""
    prepared = prepared_environment(tmp_path)
    expected = CodexHarness(CodexConfig(), command=[sys.executable], prepared_environment=prepared).process_environment()
    prepared["cache"][field] = value
    harness = CodexHarness(CodexConfig(), command=[sys.executable], prepared_environment=prepared)
    assert harness.process_environment() == expected
    assert not set(LEAN_VARIABLES).intersection(expected)


PEER = r"""
import json,os,sys
from pathlib import Path
log=Path(sys.argv[1])
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if method=='initialize':
        value={'userAgent':'Codex Desktop/0.160.0'}
    elif method in ('thread/start','thread/resume'):
        with log.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'method':method,'params':message['params'],
                'env':{key:os.environ.get(key) for key in ('PATH','LEAN','LAKE','LEAN_PATH','LEAN_SRC_PATH','LEAN_SYSROOT','LAKE_HOME','FORMAL_GEYSER_PYTHON')}})+'\n')
        value={'thread':{'id':message['params'].get('threadId','native-thread')},'model':'scripted'}
    elif method=='turn/start':
        raise AssertionError('Environment verification must not start inference')
    else:
        continue
    print(json.dumps({'id':message['id'],'result':value}),flush=True)
"""


@pytest.mark.parametrize("mode", ["start", "resume"])
@pytest.mark.parametrize("read_only", [False, True])
async def test_native_workers_and_helpers_share_pinned_paths_without_dependency_write_access(tmp_path, mode, read_only):
    prepared = prepared_environment(tmp_path)
    script = tmp_path / "peer.py"
    script.write_text(PEER, encoding="utf-8")
    log = tmp_path / "seen.jsonl"
    workspace = tmp_path / "worker" / "work"
    config = CodexConfig(version="0.160.0", home=str(tmp_path / "codex-home"))
    harness = CodexHarness(config, command=[sys.executable, "-u", str(script), str(log)],
                           prepared_environment=prepared)
    try:
        await harness.open(workspace=workspace, thread_id="native-thread" if mode == "resume" else None,
                           tools=[], instructions="Synthetic environment check.", read_only=read_only)
    finally:
        await harness.close()
    assert harness.process_quiet
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert row["method"] == "thread/" + mode
    params = row["params"]
    # The app server and its shell tools share one environment, and neither has Lean or Lake.
    shell = dict(params["config"]["shell_environment_policy.set"])
    assert shell.pop("TMPDIR", None) == (None if read_only else str(workspace.resolve() / ".private" / "tmp"))
    assert shell == {key: value for key, value in row["env"].items() if key not in LEAN_VARIABLES}
    assert all(row["env"][key] is None for key in LEAN_VARIABLES)
    assert params["config"]["sqlite_home"] == str((tmp_path / "codex-home").resolve())
    assert row["env"]["LEAN_SRC_PATH"].split(os.pathsep) == [".", *prepared["cache"]["source_roots"]]
    assert row["env"]["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert params.get("sandbox") == ("read-only" if read_only else None)
    assert params.get("permissions") == (None if read_only else "taskhub-worker")
    assert params["runtimeWorkspaceRoots"] == [str(workspace.resolve())]
    assert params["approvalPolicy"] == "never"
    assert not (workspace / ".lake").exists()
    assert not (workspace / ".dependencies").exists()


async def test_prepared_environment_without_explicit_codex_home_can_open(tmp_path):
    prepared = prepared_environment(tmp_path)
    script = tmp_path / "peer.py"
    script.write_text(PEER, encoding="utf-8")
    log = tmp_path / "seen.jsonl"
    harness = CodexHarness(CodexConfig(version="0.160.0"),
                           command=[sys.executable, "-u", str(script), str(log)],
                           prepared_environment=prepared)
    try:
        await harness.open(workspace=tmp_path / "work", thread_id=None, tools=[], instructions="Check.")
    finally:
        await harness.close()
    row = json.loads(log.read_text(encoding="utf-8").strip())
    assert "sqlite_home" not in row["params"]["config"]
