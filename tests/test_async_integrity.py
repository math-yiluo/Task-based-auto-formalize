"""Large integrity checks run in a child process, never on the service's threads.

Hashing a multi-gigabyte library on service threads held the GIL against the
coordination database thread and event loop, stalling every request.
"""
from pathlib import Path

from formal_geyser.build_verification import BuildVerifier
from formal_geyser.coordination.models import Actor, Role
from formal_geyser.registry import ObjectRegistry
from formal_geyser.schemas import EnvironmentRecord, LibraryRoot
from formal_geyser.tooling import LeanTooling
from formal_geyser.verification import ProofVerifier
from tests.test_build_verification import BuildRunner


def record_library(hub, tmp_path):
    hub.artifacts["environment-1"]["data"]["library_roots"] = [
        {"path": str(tmp_path), "manifest_sha256": hub.blobs.put_json({"files": []})}
    ]


def isolated_checks(monkeypatch, tmp_path):
    observed = []

    async def validate(_blobs, location, _manifest, *, reuse_unchanged=False):
        observed.append((location, reuse_unchanged))
        return Path(tmp_path)
    monkeypatch.setattr("formal_geyser.tooling.validate_library_isolated", validate)
    return observed


async def test_tooling_library_hashes_run_in_a_child_process(proof_case, tmp_path, monkeypatch):
    hub, _, _ = proof_case
    record_library(hub, tmp_path)
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"])
    observed = isolated_checks(monkeypatch, tmp_path)
    assert await LeanTooling(environment, hub.blobs).library_paths() == [tmp_path]
    # Every LeanTooling check may reuse a verified library while its files are unchanged.
    assert observed == [(str(tmp_path), True)]


async def test_registry_environment_hashes_run_in_a_child_process(proof_case, tmp_path, monkeypatch):
    hub, _, _ = proof_case
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"]).model_copy(update={
        "library_roots": [LibraryRoot(path=str(tmp_path), manifest_sha256="0" * 64)]})
    observed = isolated_checks(monkeypatch, tmp_path)
    result = await ObjectRegistry(hub).stage(environment, Actor(role=Role.OPERATOR))
    assert result["kind"] == "lean.environment"
    assert observed == [(str(tmp_path), False)]


async def test_proof_keeps_every_integrity_check(proof_case, tmp_path, monkeypatch):
    hub, task, artifact = proof_case
    record_library(hub, tmp_path)
    observed = isolated_checks(monkeypatch, tmp_path)
    report = await ProofVerifier().validate(hub, task, artifact)
    assert report.verdict == "accepted"
    # The Lean service checks the library for the build, for the inspection and after its last
    # process; each check may reuse a verified library whose files are unchanged.
    assert observed == [(str(tmp_path), True)] * 3


async def test_build_keeps_every_integrity_check(build_case, tmp_path, monkeypatch, lean_service):
    hub, task, artifact = build_case
    record_library(hub, tmp_path)
    observed = isolated_checks(monkeypatch, tmp_path)
    lean_service.runner = BuildRunner()
    report = await BuildVerifier().validate(hub, task, artifact)
    assert report.verdict == "accepted"
    # Compilation and post-execution integrity, each reusing an unchanged verified library.
    assert observed == [(str(tmp_path), True)] * 2
