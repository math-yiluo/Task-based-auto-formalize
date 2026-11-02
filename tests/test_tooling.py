import asyncio
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from formal_geyser.cli import app
from formal_geyser.process import ProcessResult, ProcessRunner, process_environment
from formal_geyser.tooling import parse_diagnostics


async def test_process_success(tmp_path):
    result = await ProcessRunner().run([str(Path(sys.executable).resolve()), "-c", "print('metadata')"],
                                       cwd=tmp_path, timeout=10)
    assert result.ok
    assert result.stdout.strip() == "metadata"


async def test_timeout_terminates_process(tmp_path):
    result = await ProcessRunner().run([str(Path(sys.executable).resolve()), "-c", "import time; time.sleep(30)"],
                                       cwd=tmp_path, timeout=0.2)
    assert result.timed_out
    assert not result.ok
    assert result.duration_seconds < 10


def test_check_time_is_unlimited_by_default_and_accepts_explicit_long_timeouts():
    from formal_geyser.schemas import ExecutionLimits
    assert ExecutionLimits().timeout_seconds is None
    assert ExecutionLimits(timeout_seconds=7200).timeout_seconds == 7200


async def test_unlimited_process_is_cancelled_with_its_owned_tree(tmp_path, monkeypatch):
    from formal_geyser.process import OwnedProcessHost
    started, stopped = [], []
    original_start, original_stop = OwnedProcessHost.start, OwnedProcessHost.stop

    async def start(*args, **kwargs):
        host = await original_start(*args, **kwargs)
        started.append(host)
        return host

    async def stop(host, *args, **kwargs):
        outcome = await original_stop(host, *args, **kwargs)
        stopped.append(outcome)
        return outcome

    monkeypatch.setattr(OwnedProcessHost, "start", start)
    monkeypatch.setattr(OwnedProcessHost, "stop", stop)
    marker = tmp_path / "started"
    code = "from pathlib import Path; import time; Path('started').write_text('ready'); time.sleep(120)"
    operation = asyncio.create_task(ProcessRunner().run([str(Path(sys.executable).resolve()), "-c", code], cwd=tmp_path))
    async with asyncio.timeout(10):
        while not marker.exists():
            await asyncio.sleep(0.01)
    assert not operation.done()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert started[0].process.returncode is not None
    assert stopped and all(result.tree_quiet for result in stopped)




async def test_combined_output_limit(tmp_path):
    code = "import sys; sys.stdout.write('x'*10000); sys.stdout.flush(); sys.stderr.write('y'*10000)"
    result = await ProcessRunner().run([str(Path(sys.executable).resolve()), "-c", code],
                                       cwd=tmp_path, output_limit=1024, timeout=10)
    assert result.output_limited
    assert not result.ok
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024


async def test_absolute_executable_required(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        await ProcessRunner().run(["lean", "--version"], cwd=tmp_path)


def test_lean_overrides_not_inherited(monkeypatch):
    monkeypatch.setenv("LEAN_SYSROOT", "untrusted")
    monkeypatch.setenv("LEAN_PATH", "untrusted")
    monkeypatch.setenv("LAKE_OVERRIDE_LEAN", "untrusted")
    environment = process_environment()
    assert "LEAN_SYSROOT" not in environment
    assert environment["LEAN_PATH"] == ""
    assert "LAKE_OVERRIDE_LEAN" not in environment


def test_json_diagnostics_keep_messages_and_non_json_output():
    result = ProcessResult(("synthetic",), 1,
                           '{"severity":"warning","data":"review this"}\nother output\n',
                           '{"severity":"error","data":"failed"}\n', 0.1)
    diagnostics = parse_diagnostics(result)
    assert diagnostics["errors"] == diagnostics["warnings"] == 1
    assert diagnostics["other_output"] == [{"stream": "stdout", "text": "other output"}]


def test_cli_help_and_schema():
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["schema", "statement"])
    assert result.exit_code == 0
    assert '"statement"' in result.stdout and "target_type_sha256" not in result.stdout
    proof = runner.invoke(app, ["schema", "proof"])
    assert proof.exit_code == 0 and "statement_declaration" in proof.stdout
    assert runner.invoke(app, ["schema", "statement-binding"]).exit_code != 0
    assert runner.invoke(app, ["schema", "unknown"]).exit_code != 0


@pytest.mark.parametrize("name", ["Inspect.lean", "Inspect.md", "KernelReplay.lean", "KernelReplay.md"])
def test_packaged_lean_utility_matches_review_source(name):
    package = Path(__file__).resolve().parents[1]
    assert (package / "lean-tools" / name).read_bytes() == (package / "src/formal_geyser/lean_tools" / name).read_bytes()

def test_cli_import_and_registry_use_real_core_without_model_calls(tmp_path):
    import json

    from formal_geyser.coordination.config import initialize
    from formal_geyser.coordination.models import ProjectConfig
    initialize(tmp_path, ProjectConfig(domain="lean"))
    source = tmp_path / "source.txt"
    source.write_text("CLI metadata fixture.", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["--project", str(tmp_path), "source", "import", str(source)])
    assert result.exit_code == 0, result.output
    artifact = json.loads(result.stdout)
    assert Path(artifact["source_path"]).read_bytes() == source.read_bytes()
    assert "accepted" not in artifact and "data" not in artifact
    listed = runner.invoke(app, ["--project", str(tmp_path), "registry", "list", "--object-type", "statement"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout) == []









@pytest.mark.parametrize("written", ["all", "result only", "nothing"])
async def test_inspection_fingerprints_exact_expressions_of_any_size(proof_case, tmp_path, written):
    import hashlib
    import json

    from formal_geyser.schemas import EnvironmentRecord, ExecutionLimits
    from formal_geyser.tooling import LeanTooling

    hub, _, _ = proof_case
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"]).model_copy(
        update={"local_execution": "trusted-code-only", "limits": ExecutionLimits(output_bytes=1024)})
    # Past the 64 MiB that bounded a result file, as an instance-heavy statement's expressions were.
    block, blocks = b"long expression " * 65536, 65
    expected = hashlib.sha256()
    for _ in range(blocks):
        expected.update(block)
    type_sha = expected.copy().hexdigest()
    expected.update(b"target")

    def expression(path, tail=b""):
        with path.open("wb") as stream:
            for _ in range(blocks):
                stream.write(block)
            stream.write(tail)

    class Inspector:
        async def run(self, argv, **kwargs):
            assert argv[-2] == "--output" and kwargs["output_limit"] == 1024
            output = Path(argv[-1])
            assert not output.resolve().is_relative_to(tmp_path.resolve())
            if written == "all":
                expression(output.with_name(output.name + ".type_repr"))
                expression(output.with_name(output.name + ".statement_target_repr"), b"target")
            if written != "nothing":
                output.write_text(json.dumps({"protocol": "task-based-lean-inspect@3",
                    "type_encoding": "repr", "statement_target_encoding": "repr",
                    "declaration": "Fixture.target", "kind": "definition",
                    "axioms": [], "axiom_modules": {}, "module": "Fixture", "module_axioms": [], "declared_axioms": [],
                    "statement_returns_prop": True, "target_matches": False}), encoding="utf-8")
            return ProcessResult(tuple(argv), 0, "", "", 0.01)

    result = await LeanTooling(environment, runner=Inspector()).inspect(tmp_path, "Fixture", "Fixture.target")
    assert result["ok"] is (written == "all")
    if written == "all":
        metadata = result["metadata"]
        assert (metadata["type_sha256"], metadata["type_bytes"]) == (type_sha, blocks * len(block))
        assert (metadata["statement_target_sha256"], metadata["statement_target_bytes"]) == (
            expected.hexdigest(), blocks * len(block) + 6)
        assert len(json.dumps(result)) < 4096
    else:
        assert result["unreadable_output"]
    assert not list(tmp_path.glob("inspect-*"))
