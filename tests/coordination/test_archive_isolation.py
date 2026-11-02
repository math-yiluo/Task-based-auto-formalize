"""Archived contributor support cannot become an active runtime or package input."""
from __future__ import annotations

import ast
import importlib
import importlib.resources
import importlib.util
import sys
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "formal_geyser"
PACKAGE_ROOT = PROJECT_ROOT / "src" / PACKAGE_NAME
ARCHIVE_ROOT = PROJECT_ROOT / "archive" / "external-contributors" / "2026-10-09" / "original"

# Retired files; the historical archive itself is not part of this repository.
RETIRED_FILES = (
    "src/formal_geyser/coordination/external.py",
    "src/formal_geyser/coordination/sync_protocol.py",
    "src/formal_geyser/prompts/dots.md",
    "docs/dots-launch-prompt.md",
    "docs/dots-prompt-template.md",
    "tests/test_external_records.py",
    "tests/coordination/test_external_contributions.py",
    "tests/coordination/test_external_review_runtime.py",
)
RETIRED_MODULES = {
    PACKAGE_NAME + ".external_records",
    PACKAGE_NAME + ".coordination.external",
    PACKAGE_NAME + ".coordination.sync_protocol",
}


@pytest.mark.parametrize("relative_path", RETIRED_FILES)
def test_retired_files_are_absent_from_the_active_tree(relative_path):
    assert not (PROJECT_ROOT / relative_path).exists()


@pytest.mark.parametrize("module_name", sorted(RETIRED_MODULES))
def test_retired_module_names_are_not_importable(module_name):
    assert importlib.util.find_spec(module_name) is None
    with pytest.raises(ModuleNotFoundError) as missing:
        importlib.import_module(module_name)
    assert missing.value.name == module_name


def test_native_entrypoints_and_resources_do_not_load_archived_code():
    for suffix in (
        "cli", "plugin", "submission_records", "coordination.api",
        "coordination.cli", "coordination.git_sync", "coordination.json_io",
        "coordination.runtime.runner",
    ):
        module = importlib.import_module(PACKAGE_NAME + "." + suffix)
        assert Path(module.__file__).resolve().is_relative_to(PACKAGE_ROOT)
    assert not importlib.resources.files(PACKAGE_NAME).joinpath("prompts/dots.md").is_file()
    for module in tuple(sys.modules.values()):
        location = getattr(module, "__file__", None)
        if location:
            assert not Path(location).resolve().is_relative_to(ARCHIVE_ROOT)


def test_active_source_imports_neither_archive_nor_retired_modules():
    for source in PACKAGE_ROOT.rglob("*.py"):
        parts = source.relative_to(PACKAGE_ROOT).with_suffix("").parts
        package = ".".join((PACKAGE_NAME, *parts[:-1]))
        tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package,
                )
                names = [base, *(base + "." + alias.name for alias in node.names)]
            for name in names:
                assert name != "archive" and not name.startswith("archive."), source
                assert not any(name == retired or name.startswith(retired + ".")
                               for retired in RETIRED_MODULES), (source, name)


def test_archive_is_outside_default_test_and_distribution_inputs(pytestconfig, request):
    testpaths = pytestconfig.getini("testpaths")
    assert testpaths
    for path in testpaths:
        selected = (PROJECT_ROOT / path).resolve()
        assert not ARCHIVE_ROOT.is_relative_to(selected)
        assert not selected.is_relative_to(PROJECT_ROOT / "archive")
    assert all(not Path(item.path).resolve().is_relative_to(PROJECT_ROOT / "archive")
               for item in request.session.items)

    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build = config["tool"]["hatch"]["build"]
    wheel = build["targets"]["wheel"]
    assert wheel["packages"]
    for path in wheel["packages"]:
        selected = (PROJECT_ROOT / path).resolve()
        assert not ARCHIVE_ROOT.is_relative_to(selected)
        assert not selected.is_relative_to(PROJECT_ROOT / "archive")
    # Source distributions must also exclude the historical tree explicitly.
    sdist = build["targets"].get("sdist", {})
    exclusions = {*build.get("exclude", []), *sdist.get("exclude", [])}
    assert exclusions & {"archive", "/archive", "archive/**", "/archive/**"}
    for section in (build, wheel, sdist):
        for path in section.get("force-include", {}):
            selected = (PROJECT_ROOT / path).resolve()
            assert not ARCHIVE_ROOT.is_relative_to(selected)
            assert not selected.is_relative_to(PROJECT_ROOT / "archive")


async def test_fresh_hub_rejects_external_import_without_contributor_tables(tmp_path):
    hub = Hub(tmp_path / "native-project", config=ProjectConfig())
    try:
        tables = await hub.db.call(lambda connection: {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        })
        assert {"external_receipts", "external_records", "native_external_notes"}.isdisjoint(tables)
        # Human and literature response waits remain part of the native runtime.
        assert {"external_requests", "external_request_waiters"} <= tables
        with pytest.raises(HubError) as rejected:
            await hub.execute(
                Command(name="external.import", key=uuid4().hex, payload={}),
                Actor(role=Role.OPERATOR),
            )
        assert rejected.value.code == "UNKNOWN_COMMAND"
    finally:
        await hub.close()
