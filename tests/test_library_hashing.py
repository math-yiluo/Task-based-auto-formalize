"""Parallel hashing must preserve complete content and file-set integrity."""
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from formal_geyser import registry
from formal_geyser.coordination.storage import BlobStore
from formal_geyser.registry import RegistryError, library_manifest, validate_library


def make_library(tmp_path, count=20):
    root = tmp_path / "library"
    root.mkdir()
    for index in range(count):
        (root / f"file{index:03}.olean").write_bytes(f"software fixture {index}".encode())
    return root


def test_parallel_manifest_matches_sequential_content_and_order(tmp_path):
    root = make_library(tmp_path)
    expected = {"files": [{"path": path.relative_to(root).as_posix(),
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                          for path in sorted(root.rglob("*")) if path.is_file()]}
    assert library_manifest(root) == expected
    assert library_manifest(root) == expected
    blobs = BlobStore(tmp_path / "blobs")
    assert validate_library(blobs, str(root), blobs.put_json(expected)) == root


def test_every_file_is_hashed_with_bounded_concurrency(tmp_path, monkeypatch):
    root = make_library(tmp_path, count=72)
    original = registry.file_sha256
    lock = threading.Lock()
    state = {"active": 0, "maximum": 0}
    observed = []
    def hashing(path):
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        try:
            time.sleep(0.005)
            result = original(path)
            with lock:
                observed.append(path.name)
            return result
        finally:
            with lock:
                state["active"] -= 1
    monkeypatch.setattr(registry, "file_sha256", hashing)
    manifest = library_manifest(root)
    assert len(manifest["files"]) == len(set(observed)) == 72
    assert 1 < state["maximum"] <= registry.LIBRARY_HASH_WORKERS
    assert state["active"] == 0


def test_hash_queue_never_prefetches_the_whole_library(tmp_path, monkeypatch):
    root = make_library(tmp_path, count=13)
    submitted = []
    class RecordingExecutor(ThreadPoolExecutor):
        def submit(self, function, *args, **kwargs):
            submitted.append(args[-1])
            return super().submit(function, *args, **kwargs)
    monkeypatch.setattr(registry, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(registry, "LIBRARY_HASH_BATCH", 5)
    monkeypatch.setattr(registry, "LIBRARY_HASH_WORKERS", 2)
    hashes = registry._library_hashes(root, [path.name for path in root.iterdir()])
    try:
        next(hashes)
        assert len(submitted) == 5
        for _ in range(4):
            next(hashes)
        assert len(submitted) == 5
        next(hashes)
        assert len(submitted) == 10
    finally:
        hashes.close()
    assert len(submitted) == 10


def test_validation_rehashes_equal_size_equal_timestamp_changes(tmp_path):
    root = make_library(tmp_path, count=1)
    source = next(root.iterdir())
    original = source.stat()
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json(library_manifest(root))
    source.write_bytes(b"x" * original.st_size)
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(RegistryError, match="has changed"):
        validate_library(blobs, str(root), sha)


@pytest.mark.parametrize("change", ["add", "remove"])
def test_validation_preserves_exact_file_set(tmp_path, change):
    root = make_library(tmp_path, count=3)
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json(library_manifest(root))
    if change == "add":
        (root / "extra.olean").write_bytes(b"unexpected")
    else:
        (root / "file000.olean").unlink()
    with pytest.raises((RegistryError, FileNotFoundError)):
        validate_library(blobs, str(root), sha)


def test_worker_read_failures_propagate_and_pool_finishes(tmp_path, monkeypatch):
    root = make_library(tmp_path, count=12)
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json(library_manifest(root))
    original = registry.file_sha256
    def failing(path):
        if path.name == "file002.olean":
            raise PermissionError("Synthetic read failure")
        return original(path)
    monkeypatch.setattr(registry, "file_sha256", failing)
    with pytest.raises(PermissionError, match="Synthetic read failure"):
        library_manifest(root)
    with pytest.raises(PermissionError, match="Synthetic read failure"):
        validate_library(blobs, str(root), sha)
    assert not [thread for thread in threading.enumerate() if thread.name.startswith("lean-library-hash")]


def test_manifest_traversal_is_rejected_before_hash_submission(tmp_path, monkeypatch):
    root = make_library(tmp_path, count=1)
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json({"files": [{"path": "../outside.olean", "sha256": "0" * 64}]})
    called = []
    monkeypatch.setattr(registry, "file_sha256", lambda path: called.append(path) or "0" * 64)
    with pytest.raises(ValueError):
        validate_library(blobs, str(root), sha)
    assert not called


def test_linked_directory_is_rejected_during_capture_and_validation(tmp_path):
    root = make_library(tmp_path, count=1)
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json(library_manifest(root))
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RegistryError, match="linked"):
        library_manifest(root)
    with pytest.raises(RegistryError, match="linked"):
        validate_library(blobs, str(root), sha)


def test_library_manifest_requires_real_directory(tmp_path):
    with pytest.raises(RegistryError, match="existing directory"):
        library_manifest(tmp_path / "missing")

def test_unreadable_subtree_is_not_silently_omitted(tmp_path, monkeypatch):
    root = make_library(tmp_path, count=1)
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json({"files": []})
    def failing_scandir(path):
        raise PermissionError("Synthetic directory enumeration failure")
    monkeypatch.setattr(registry.os, "scandir", failing_scandir)
    with pytest.raises(PermissionError, match="enumeration failure"):
        library_manifest(root)
    with pytest.raises(PermissionError, match="enumeration failure"):
        validate_library(blobs, str(root), sha)


def counting_hashes(monkeypatch):
    calls = []
    original = registry.file_sha256
    monkeypatch.setattr(registry, "file_sha256", lambda path: calls.append(path) or original(path))
    return calls


def test_development_revalidation_reuses_unchanged_files(tmp_path, monkeypatch):
    root = make_library(tmp_path, count=4)
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json(library_manifest(root))
    calls = counting_hashes(monkeypatch)
    validate_library(blobs, str(root), sha, reuse_unchanged=True)
    assert len(calls) == 4  # The first check in a process reads every byte.
    validate_library(blobs, str(root), sha, reuse_unchanged=True)
    assert len(calls) == 4
    validate_library(blobs, str(root), sha)
    assert len(calls) == 8  # Acceptance paths always re-hash.


def test_development_revalidation_rehashes_after_a_visible_change(tmp_path, monkeypatch):
    root = make_library(tmp_path, count=2)
    blobs = BlobStore(tmp_path / "blobs")
    sha = blobs.put_json(library_manifest(root))
    validate_library(blobs, str(root), sha, reuse_unchanged=True)
    source = root / "file000.olean"
    source.write_bytes(b"changed library content")
    calls = counting_hashes(monkeypatch)
    with pytest.raises(RegistryError, match="has changed"):
        validate_library(blobs, str(root), sha, reuse_unchanged=True)
    assert calls
