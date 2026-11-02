"""Packaging and runtime checks that never need a separate coordination project."""
from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import json
import re
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import tomllib
from pathlib import Path

from formal_geyser.coordination.plugins import load_plugin
from formal_geyser.coordination.runtime import process as process_module
from formal_geyser.plugin import LeanPlugin

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "formal_geyser"


def _distribution_name(requirement: str) -> str:
    name = re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def test_distribution_owns_both_commands_without_external_core_dependency():
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    installed = importlib.metadata.distribution("formal-geyser")
    requirements = [*config["project"]["dependencies"], *(installed.requires or [])]
    assert "task-based-core" not in {_distribution_name(item) for item in requirements}
    sources = config.get("tool", {}).get("uv", {}).get("sources", {})
    assert "task-based-core" not in {_distribution_name(name) for name in sources}
    commands = {
        item.name: item.value for item in installed.entry_points
        if item.group == "console_scripts"
    }
    assert commands["taskctl"] == "formal_geyser.coordination.cli:app"
    assert commands["formalizectl"] == "formal_geyser.cli:app"


def test_lean_plugin_is_builtin_and_comes_from_this_application(monkeypatch):
    def forbidden_discovery(**kwargs):
        raise AssertionError("Built-in Lean must not need external plugin discovery")

    monkeypatch.setattr(
        "formal_geyser.coordination.plugins.metadata.entry_points",
        forbidden_discovery,
    )
    plugin = load_plugin("lean")
    assert type(plugin) is LeanPlugin
    assert Path(inspect.getfile(type(plugin))).resolve().is_relative_to(PACKAGE_ROOT)


def test_relocated_application_works_without_external_core_or_editable_mappings(tmp_path):
    relocated = tmp_path / "relocated"
    shutil.copytree(
        PACKAGE_ROOT, relocated / PACKAGE_ROOT.name,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    working = tmp_path / "unrelated-working-directory"
    working.mkdir()
    site_roots = {Path(sysconfig.get_path(name)).resolve() for name in ("purelib", "platlib")}
    dependency_paths = sorted({str(root) for root in site_roots} | {
        str(Path(path).resolve()) for path in sys.path
        if path and any(Path(path).resolve().is_relative_to(root) for root in site_roots)
    })
    program = textwrap.dedent("""\
        import importlib
        import importlib.abc
        import importlib.metadata as metadata
        import importlib.util
        import json
        import os
        import pkgutil
        import sys
        from pathlib import Path

        relocated = Path(sys.argv[1]).resolve()
        sys.path[:0] = [str(relocated), *json.loads(sys.argv[2])]
        assert importlib.util.find_spec("task_based") is None
        try:
            metadata.distribution("task-based-core")
        except metadata.PackageNotFoundError:
            pass
        else:
            raise AssertionError("The isolated environment contains the external core")

        class NoExternalCore(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "task_based" or fullname.startswith("task_based."):
                    raise AssertionError("External core import attempted: " + fullname)
                return None

        sys.meta_path.insert(0, NoExternalCore())

        def forbidden_discovery(**kwargs):
            raise AssertionError("Built-in domains cannot need installed plugin metadata")

        metadata.entry_points = forbidden_discovery
        import formal_geyser as package

        for item in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            if not item.name.endswith(".__main__"):
                importlib.import_module(item.name)
        for name, module in tuple(sys.modules.items()):
            if name == package.__name__ or name.startswith(package.__name__ + "."):
                assert Path(module.__file__).resolve().is_relative_to(relocated)
        assert not any(name == "task_based" or name.startswith("task_based.")
                       for name in sys.modules)

        from typer.testing import CliRunner
        from formal_geyser.cli import app as formalizectl
        from formal_geyser.coordination.cli import app as taskctl
        from formal_geyser.coordination.config import initialize
        from formal_geyser.coordination.models import ProjectConfig
        from formal_geyser.coordination.plugins import load_plugin
        from formal_geyser.coordination.runtime import process
        from formal_geyser.plugin import LeanPlugin

        lean = load_plugin("lean")
        assert type(lean) is LeanPlugin
        assert load_plugin("general").name == "general"
        project = Path.cwd() / "fresh-project"
        initialize(project, ProjectConfig(domain="lean"))
        contract = Path.cwd() / "contract.json"
        contract.write_text(json.dumps({
            "title": "Inspect software metadata",
            "objective": "Exercise the local publication protocol only.",
            "domain": "lean",
            "acceptance_policy": "lean.peer@1",
        }), encoding="utf-8")
        runner = CliRunner()
        published = runner.invoke(taskctl, [
            "--project", str(project), "tasks", "publish", str(contract)])
        assert published.exit_code == 0, (published.output, published.exception)
        listed = runner.invoke(taskctl, ["--project", str(project), "tasks", "list"])
        assert listed.exit_code == 0, (listed.output, listed.exception)
        tasks = json.loads(listed.stdout)
        assert len(tasks) == 1 and tasks[0]["id"] == json.loads(published.stdout)["id"]
        schema = runner.invoke(formalizectl, ["schema", "statement"])
        assert schema.exit_code == 0, (schema.output, schema.exception)
        assert "statement" in json.loads(schema.stdout)["properties"]
        print(json.dumps({"plugin_module": type(lean).__module__, "task_count": len(tasks)}))
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", program, str(relocated), json.dumps(dependency_paths)],
        cwd=working, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "plugin_module": "formal_geyser.plugin", "task_count": 1,
    }


async def test_owned_process_runs_the_command_itself_once_in_its_cgroup(tmp_path, monkeypatch):
    original_start = asyncio.create_subprocess_exec
    commands = []

    async def capture_start(*command, **kwargs):
        commands.append(command)
        return await original_start(*command, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_start)
    command = [sys.executable, "-c", "print('owned-process')"]
    host = await process_module.OwnedProcessHost.start(command, cwd=tmp_path)
    try:
        stdout, stderr = await asyncio.wait_for(host.process.communicate(), timeout=10)
        assert host.process.returncode == 0, stderr.decode()
        assert stdout.strip() == b"owned-process"
    finally:
        stopped = await host.stop(grace=0.2)
    assert stopped.tree_quiet
    # A shell that moves itself into the cgroup, then executes the command in its place.
    assert commands[0][0] == "/bin/sh" and list(commands[0][-len(command):]) == command
