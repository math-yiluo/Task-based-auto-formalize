"""Integration capture preserves exact bytes without rewriting unchanged Git objects."""
import os
import shutil

import pytest

from formal_geyser.coordination.repository import RepositoryError, RepositoryStore
from formal_geyser.coordination.storage import BlobStore
from tests.coordination.test_repository import contents, manifest, workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


@pytest.fixture
def prepared(tmp_path):
    blobs = BlobStore(tmp_path / "state/blobs")
    repository = RepositoryStore(tmp_path / "state")
    files = {f"part{index:02}.txt": f"original {index}\n".encode() for index in range(12)}
    base = repository.initialize(blobs, manifest(blobs, files))["head"]
    work = workspace(tmp_path, "review")
    candidate = {"part00.txt": b"candidate bytes\r\n"}
    repository.prepare(blobs, manifest(blobs, candidate), base, work, list(candidate))
    return repository, blobs, work, {**files, **candidate}


def track_hashes(repository, monkeypatch):
    calls = []
    original = repository._hash_bytes
    def tracked(body, directory):
        calls.append(body)
        return original(body, directory)
    monkeypatch.setattr(repository, "_hash_bytes", tracked)
    return calls


def test_freeze_reuses_exact_index_bytes_without_per_file_git_writes(prepared, monkeypatch):
    repository, blobs, work, expected = prepared
    calls = track_hashes(repository, monkeypatch)
    result = repository.finalize(work, blobs)
    assert calls == []
    assert contents(blobs, result["snapshot"]) == expected
    assert repository.publish(result["commit"], result["target_head"])["published"]


def test_only_changed_bytes_are_rewritten_even_when_size_and_mtime_match(prepared, monkeypatch):
    repository, blobs, work, expected = prepared
    path = work / "part01.txt"
    original = path.stat()
    replacement = b"modified 1\n"
    assert len(replacement) == len(expected[path.name])
    path.write_bytes(replacement)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    calls = track_hashes(repository, monkeypatch)
    result = repository.finalize(work, blobs)
    assert calls == [replacement]
    assert contents(blobs, result["snapshot"]) == {**expected, path.name: replacement}


def test_deletions_and_new_binary_files_keep_exact_content(prepared):
    repository, blobs, work, expected = prepared
    (work / "part01.txt").unlink()
    (work / "binary.dat").write_bytes(b"\x00\xff\r\n")
    repository.resolve(work, ["part01.txt", "binary.dat"])
    result = repository.finalize(work, blobs)
    expected.pop("part01.txt")
    assert contents(blobs, result["snapshot"]) == {**expected, "binary.dat": b"\x00\xff\r\n"}


def test_unchanged_executable_entry_is_still_normalized(prepared, monkeypatch):
    repository, blobs, work, expected = prepared
    entries, _ = repository._index(work)
    sha = entries["part01.txt"][1]
    repository._git(["update-index", "--cacheinfo", f"100755,{sha},part01.txt"],
                    git_dir=work / ".git", workspace=work)
    calls = track_hashes(repository, monkeypatch)
    result = repository.finalize(work, blobs)
    assert calls == []
    assert repository._index(work)[0]["part01.txt"][0] == "100644"
    assert contents(blobs, result["snapshot"]) == expected


def test_corrupt_existing_git_object_cannot_be_reused(prepared):
    repository, blobs, work, _ = prepared
    entries, _ = repository._index(work)
    sha = entries["part01.txt"][1]
    loose = repository.path / "objects" / sha[:2] / sha[2:]
    loose.chmod(0o600)
    loose.write_bytes(b"invalid compressed object")
    with pytest.raises(RepositoryError):
        repository.finalize(work, blobs)


def test_reusing_an_empty_blob_still_clears_intent_to_add(prepared):
    repository, blobs, work, expected = prepared
    (work / "empty.txt").write_bytes(b"")
    repository._git(["add", "--intent-to-add", "--", "empty.txt"],
                    git_dir=work / ".git", workspace=work)
    repository.resolve(work, ["empty.txt"])
    result = repository.finalize(work, blobs)
    assert contents(blobs, result["snapshot"]) == {**expected, "empty.txt": b""}
