"""Tiny source/export envelopes exercise real local Git without large files."""
from __future__ import annotations

import shutil

import pytest

from formal_geyser.coordination import git_transport
from formal_geyser.coordination.git_transport import GitSyncError, GitSyncTransport
from formal_geyser.coordination.repository import RepositoryError, RepositoryStore
from formal_geyser.coordination.storage import BlobStore
from tests.coordination.test_git_transport import git, manifest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


@pytest.fixture
def envelope(tmp_path, monkeypatch):
    for name, value in {
        "MAX_SNAPSHOT_FILES": 2, "MAX_SNAPSHOT_BYTES": 8,
        "_MAX_STATE_FILES": 2, "_MAX_STATE_BYTES": 4,
        "_MAX_SYNC_FILES": 4, "_MAX_SYNC_BYTES": 12,
        # Real Git metadata has a separate physical cost from the tiny bodies.
        "_MAX_CACHE_BYTES": 1024 * 1024,
    }.items():
        monkeypatch.setattr(git_transport, name, value)
    remote = tmp_path / "remote.git"
    git("init", "--bare", "--template=", "--initial-branch=main", str(remote))
    transport = GitSyncTransport(tmp_path / "transport", str(remote))
    transport.initialize()
    # Deliberately larger than one source envelope so publish_project itself
    # must enforce the distinct source allowance before adding generated state.
    repository = RepositoryStore(tmp_path / "source", max_files=4, max_bytes=12)
    blobs = BlobStore(tmp_path / "blobs")
    return transport, repository, blobs


async def test_full_source_budget_plus_full_generated_state_publishes_locally(envelope):
    transport, repository, blobs = envelope
    source = {"source-a": b"1234", "source-b": b"5678"}
    state = {"state-a.json": b"ab", "state-b.json": b"cd"}
    source_commit = repository.initialize(blobs, manifest(blobs, source))["head"]
    prepared = []

    async def save(commit):
        prepared.append(commit)

    result = await transport.publish_project(repository, source_commit, "main", state, None,
                                             "Synthetic complete export\n", save)
    assert result["changed"] and prepared == [result["head"]]
    assert transport.files(result["head"]) == {
        **source, **{"taskhub/" + name: body for name, body in state.items()},
    }
    assert await transport.fetch("main") == result["head"]
    assert repository.head() == source_commit


@pytest.mark.parametrize("boundary", ["state_bytes", "state_files", "source_bytes", "source_files"])
async def test_source_and_generated_state_cannot_consume_each_others_allowance(envelope, monkeypatch, boundary):
    transport, repository, blobs = envelope
    source = {"source-a": b"1234", "source-b": b"5678"}
    state = {"state.json": b"ab"}
    if boundary == "state_bytes":
        source = {"source-a": b"a"}  # Spare source capacity cannot enlarge the state allowance.
        state = {"state.json": b"12345"}
    elif boundary == "state_files":
        source = {"source-a": b"a"}
        state = {"a": b"a", "b": b"b", "c": b"c"}
    elif boundary == "source_bytes":
        source = {"source-a": b"12345", "source-b": b"6789"}
    else:
        source = {"source-a": b"a", "source-b": b"b", "source-c": b"c"}
    commit = repository.initialize(blobs, manifest(blobs, source))["head"]

    async def unexpected(*args, **kwargs):
        raise AssertionError("An oversized export reached the remote or prepared a publication.")

    monkeypatch.setattr(transport, "_remote_git", unexpected)
    with pytest.raises((GitSyncError, RepositoryError)):
        await transport.publish_project(repository, commit, "main", state, None,
                                        "Oversized synthetic export\n", unexpected)
    assert not list((transport.control / "publications").iterdir())
