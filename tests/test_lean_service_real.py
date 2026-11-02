"""The Lean service with a real Lean toolchain and the real Codex sandbox, on a tiny module.

Set FORMAL_GEYSER_TEST_LEAN to a pinned lean executable (its lake beside it) to run it; it needs
no Mathlib and calls no model.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from formal_geyser.coordination.models import LeanServiceConfig
from formal_geyser.coordination.storage import BlobStore
from formal_geyser.lean_service import client
from formal_geyser.lean_service.sandbox import CodexSandbox
from formal_geyser.lean_service.service import LeanService
from formal_geyser.registry import file_sha256
from formal_geyser.schemas import EnvironmentRecord
from tests.lean_service_helpers import LocalClient
from tests.test_lean_service import CODEX

LEAN = Path(os.environ.get("FORMAL_GEYSER_TEST_LEAN", "missing"))
pytestmark = pytest.mark.skipif(not LEAN.is_file() or CODEX is None,
                                reason="set FORMAL_GEYSER_TEST_LEAN; the audited Codex in .tools")
SOURCE = "def Probe.value : Nat := 2 + 2\n\ntheorem Probe.fact : Probe.value = 4 := rfl\n"


def environment():
    version = subprocess.run([str(LEAN), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    lake = LEAN.with_name("lake")
    return EnvironmentRecord(name="Core Lean", lean_executable=str(LEAN), lean_executable_sha256=file_sha256(LEAN),
                             lean_version=version, lake_executable_sha256=file_sha256(lake),
                             local_execution="trusted-code-only")


def spec(record, blobs, *, lane, snapshot=None, directory=None):
    return {"kind": "check", "lane": lane, "owner": "w1", "label": "real", "blocking": True,
            "request": {"environment": record.model_dump(mode="json"), "blobs": str(blobs.root), "snapshot": snapshot,
                        "directory": None if directory is None else str(directory), "modules": ["Probe"],
                        "kernel_replay": snapshot is not None,
                        "inspections": [{"module": "Probe", "declaration": "Probe.value"}]}}


async def test_real_checks_build_replay_and_inspect_in_the_sandbox():
    # Not below %TEMP% or in a pytest directory: their ACLs differ from a project's.
    root = Path(__file__).resolve().parents[1] / ".task-based-real-test"
    shutil.rmtree(root, ignore_errors=True)
    work = root / "work"
    work.mkdir(parents=True)
    (work / "Probe.lean").write_text(SOURCE, encoding="utf-8")
    blobs = BlobStore(root / ".task-based" / "objects" / "sha256")
    record = environment()
    state = client.state_directory(root)
    service = LeanService(root, LeanServiceConfig(), sandbox=CodexSandbox(str(CODEX), state / "codex-home"))
    await service.start()
    try:
        development = await client.run(LocalClient(service), spec(record, blobs, lane="interactive", directory=work))
        assert development["doctor"]["ok"], development
        assert development["compilation"]["ok"], development["compilation"]["process"]
        assert development["inspections"][0]["ok"] and development["inspections"][0]["metadata"]["kind"] == "definition"
        assert not (work / ".lake").exists()
        entry = {"path": "Probe.lean", "sha256": blobs.put(SOURCE.encode("utf-8"))}
        snapshot = blobs.put_json({"files": [entry]})
        official = await client.run(LocalClient(service), spec(record, blobs, lane="official", snapshot=snapshot))
        assert official["compilation"]["ok"] and official["compilation"]["kernel_replay"]["ok"], official["compilation"]
        assert official["inspections"][0]["ok"] and official["definitive"]
        rows = service.store.telemetry(0)["lanes"]
        assert rows["official"]["jobs"] == 1 and rows["interactive"]["jobs"] == 1
    finally:
        await service.close()
        shutil.rmtree(root, ignore_errors=True)


async def test_a_real_resident_session_elaborates_only_what_changed():
    root = Path(__file__).resolve().parents[1] / ".task-based-real-session-test"
    shutil.rmtree(root, ignore_errors=True)
    work = root / "work"
    work.mkdir(parents=True)
    probe = work / "Probe.lean"
    probe.write_text(SOURCE, encoding="utf-8")
    blobs = BlobStore(root / ".task-based" / "objects" / "sha256")
    record = environment()
    state = client.state_directory(root)
    service = LeanService(root, LeanServiceConfig(), sandbox=CodexSandbox(str(CODEX), state / "codex-home"))
    await service.start()

    def development():
        value = spec(record, blobs, lane="interactive", directory=work)
        value["request"]["inspections"] = []
        return value

    try:
        first = (await client.run(LocalClient(service), development()))["compilation"]
        assert first["validation_method"] == "lean-server" and first["ok"], first
        assert first["session"]["reopened"] == "opened"
        probe.write_text(SOURCE + "\ntheorem Probe.wrong : Probe.value = 5 := rfl\n", encoding="utf-8")
        second = (await client.run(LocalClient(service), development()))["compilation"]
        assert second["session"]["incremental"] and not second["ok"] and second["diagnostics"]["errors"] >= 1
        assert any(message["pos"]["line"] == 5 for message in second["diagnostics"]["messages"])
        probe.write_text(SOURCE + "\ntheorem Probe.right : Probe.value = 4 := rfl\n", encoding="utf-8")
        third = (await client.run(LocalClient(service), development()))["compilation"]
        assert third["session"]["incremental"] and third["ok"] and third["diagnostics"]["errors"] == 0
        assert not (work / ".lake").exists()
    finally:
        await service.close()
        shutil.rmtree(root, ignore_errors=True)
