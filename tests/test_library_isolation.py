"""The service checks library integrity in a child process, with the same exact rules."""
import asyncio
import os

import pytest

from formal_geyser import registry, tooling
from formal_geyser.coordination.storage import BlobStore
from formal_geyser.registry import (
    EnvironmentUnavailable,
    RegistryError,
    library_manifest,
)
from formal_geyser.schemas import EnvironmentRecord, LibraryRoot
from formal_geyser.tooling import LeanTooling, validate_library_isolated
from tests.test_library_hashing import make_library


@pytest.fixture
def library(tmp_path):
    root = make_library(tmp_path, count=6)
    blobs = BlobStore(tmp_path / "blobs")
    return root, blobs, blobs.put_json(library_manifest(root))


def recorded(monkeypatch):
    responses = []
    original = tooling._run_library_check

    async def run(request):
        responses.append(await original(request))
        return responses[-1]
    monkeypatch.setattr(tooling, "_run_library_check", run)
    return responses


async def test_every_byte_is_hashed_in_another_process(library, monkeypatch):
    root, blobs, sha = library
    responses = recorded(monkeypatch)

    def no_service_threads(*_, **__):
        raise AssertionError("Library bytes were hashed on the service's own threads")
    monkeypatch.setattr(registry, "ThreadPoolExecutor", no_service_threads)
    assert await validate_library_isolated(blobs, str(root), sha) == root.resolve()
    assert responses[0]["rehashed"] and responses[0]["pid"] != os.getpid()


async def test_a_changed_library_is_reported_by_the_child(library):
    root, blobs, sha = library
    (root / "file003.olean").write_bytes(b"changed library content")
    with pytest.raises(RegistryError, match="has changed"):
        await validate_library_isolated(blobs, str(root), sha)


async def test_an_unchanged_library_is_reused_only_when_requested(library, monkeypatch):
    root, blobs, sha = library
    responses = recorded(monkeypatch)
    for reuse in (True, True, False):
        await validate_library_isolated(blobs, str(root), sha, reuse_unchanged=reuse)
    assert [response["rehashed"] for response in responses] == [True, False, True]
    (root / "file001.olean").write_bytes(b"a visibly different library file")
    with pytest.raises(RegistryError, match="has changed"):
        await validate_library_isolated(blobs, str(root), sha, reuse_unchanged=True)


async def test_an_incomplete_check_is_unavailable_not_a_verdict(library, tmp_path, proof_case):
    root, _, sha = library
    elsewhere = BlobStore(tmp_path / "other-blobs")  # The child cannot read the manifest.
    with pytest.raises(RegistryError, match="did not complete"):
        await validate_library_isolated(elsewhere, str(root), sha)
    hub, _, _ = proof_case
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"]).model_copy(update={
        "library_roots": [LibraryRoot(path=str(root), manifest_sha256=sha)]})
    with pytest.raises(EnvironmentUnavailable):
        await LeanTooling(environment, elsewhere).library_paths()


async def test_concurrent_checks_of_one_library_share_one_child(library, monkeypatch):
    root, blobs, sha = library
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def run(request):
        requests.append(request)
        started.set()
        await release.wait()
        return {"ok": True, "root": str(root.resolve()), "digest": "identities", "rehashed": True, "pid": 1}
    monkeypatch.setattr(tooling, "_run_library_check", run)
    checks = [asyncio.create_task(validate_library_isolated(blobs, str(root), sha, reuse_unchanged=True))
              for _ in range(3)]
    await started.wait()
    await asyncio.sleep(0)
    checks[0].cancel()  # One caller leaving does not stop the shared check.
    release.set()
    assert await asyncio.gather(*checks[1:]) == [root.resolve()] * 2
    assert len(requests) == 1 and not tooling._LIBRARY_CHECKS
    assert await validate_library_isolated(blobs, str(root), sha, reuse_unchanged=True) == root.resolve()
    assert len(requests) == 2  # A finished check is not reused as if it were still running.


async def test_validations_reuse_a_verified_library_until_a_file_changes(library, monkeypatch, proof_case):
    root, blobs, sha = library
    hub, _, _ = proof_case
    environment = EnvironmentRecord.model_validate(hub.artifacts["environment-1"]["data"]).model_copy(update={
        "library_roots": [LibraryRoot(path=str(root), manifest_sha256=sha)]})
    responses = recorded(monkeypatch)
    for _ in range(2):
        assert await LeanTooling(environment, blobs).library_paths() == [root.resolve()]
    assert [response["rehashed"] for response in responses] == [True, False]
    (root / "file002.olean").write_bytes(b"a library file changed after verification")
    with pytest.raises(EnvironmentUnavailable, match="has changed"):
        await LeanTooling(environment, blobs).library_paths()
    assert len(responses) == 3 and not responses[-1]["ok"]  # The change was re-hashed and rejected.
