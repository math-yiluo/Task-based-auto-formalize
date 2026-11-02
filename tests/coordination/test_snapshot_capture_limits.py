"""Physical capture accepts the full configured envelope and enforces tight bounds."""
from pathlib import Path

import pytest

from formal_geyser.coordination.repository import RepositoryError, RepositoryStore
from formal_geyser.coordination.storage import BlobStore


def test_capture_limits_are_enforced_before_snapshot_publication(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.md").write_bytes(b"first")
    (source / "second.md").write_bytes(b"second")
    blobs = BlobStore(tmp_path / "blobs", max_files=2, max_bytes=11)
    exact = blobs.snapshot(source)
    assert len(blobs.get_json(exact)["files"]) == 2
    with pytest.raises(ValueError, match="file limit"):
        blobs.snapshot(source, max_files=1)
    with pytest.raises(ValueError, match="byte limit"):
        blobs.snapshot(source, max_bytes=10)
    assert len(blobs.get_json(exact)["files"]) == 2


def test_default_capture_does_not_reapply_a_smaller_file_gate(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(2050):
        (source / f"file-{index}.md").write_bytes(b"Synthetic file-count fixture.\n")
    blobs = BlobStore(tmp_path / "blobs")
    captured = blobs.snapshot(source, max_files=4096)
    assert len(blobs.get_json(captured)["files"]) == 2050


def test_repository_manifest_uses_its_explicit_file_limit(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    digest = blobs.put(b"Synthetic source.\n")
    manifest = blobs.put_json({"files": [
        {"path": f"Library/File{index}.md", "sha256": digest} for index in range(10001)
    ]})
    repository = RepositoryStore(tmp_path / "repository", max_files=12000)
    assert len(repository._manifest(blobs, manifest)) == 10001
    smaller = RepositoryStore(tmp_path / "repository", max_files=10000)
    with pytest.raises(RepositoryError, match="configured 10000-file limit"):
        smaller._manifest(blobs, manifest)


def test_repository_content_capture_uses_the_same_byte_budget(tmp_path):
    repository = RepositoryStore(tmp_path / "repository", max_files=2, max_bytes=11)
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.md").write_bytes(b"first")
    (source / "two.md").write_bytes(b"second")
    assert repository._working_bytes(source, ["one.md", "two.md"]) == {
        "one.md": b"first", "two.md": b"second",
    }
    (source / "two.md").write_bytes(b"longest")
    with pytest.raises(RepositoryError, match="configured byte limit"):
        repository._working_bytes(source, ["one.md", "two.md"])


@pytest.mark.parametrize("kwargs", [
    {"max_files": 0}, {"max_bytes": 0}, {"max_files": True},
    {"max_files": 1.5}, {"max_bytes": float("inf")},
])
def test_invalid_capture_bounds_are_not_silently_replaced(tmp_path: Path, kwargs):
    with pytest.raises(ValueError, match="Snapshot .* limit"):
        BlobStore(tmp_path / "blobs", **kwargs)
    with pytest.raises(ValueError, match="Snapshot .* limit"):
        RepositoryStore(tmp_path / "repository", **kwargs)
