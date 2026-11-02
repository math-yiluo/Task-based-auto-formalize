"""Large fallback files stay bounded, exact, confined, and atomically editable."""
from __future__ import annotations

import hashlib
import json
import stat
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import pytest

from formal_geyser.coordination.dag_check import parse_graph, validate_dags
from formal_geyser.coordination.runtime import files as file_module
from formal_geyser.coordination.runtime.files import ScopedFiles


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def trees(tmp_path):
    resource, workspace = tmp_path / "resource", tmp_path / "workspace"
    resource.mkdir()
    workspace.mkdir()
    return ScopedFiles(resource), ScopedFiles(workspace)


def assert_no_temporary(tree):
    assert not list(tree.root.rglob(".task-based-write-*"))


def test_copy_and_edit_valid_dag_larger_than_one_write_without_changing_source(trees):
    source, destination = trees
    text = json.dumps({"nodes": [{"id": "D", "title": "Old title",
        "description": "α🙂" + "x" * (1024 * 1024)}], "edges": []}, ensure_ascii=False)
    body = text.encode("utf-8")
    shared = source.root / "definitions.json"
    shared.write_bytes(body)
    shared.chmod(stat.S_IREAD)
    relative = ".private/dags/edit/definitions.json"
    copied = destination.copy_from(source, "definitions.json", relative,
                                   expected_source_sha256=sha(body))
    assert copied["bytes"] > 1024 * 1024
    assert copied["sha256"] == copied["source_sha256"] == sha(body)
    offset = text.index("Old title")
    edited = destination.write(relative, "New title λ", copied["sha256"],
                               offset=offset, delete_count=len("Old title"))
    final = (destination.root / relative).read_bytes()
    expected = text[:offset] + "New title λ" + text[offset + len("Old title"):]
    assert final == expected.encode("utf-8")
    assert edited["sha256"] == sha(final)
    assert shared.read_bytes() == body
    validate_dags(parse_graph(final, "definitions.json"), {"nodes": [], "edges": []})
    assert_no_temporary(destination)


def test_incremental_insertions_can_build_a_large_file(trees):
    _, files = trees
    first = files.write("large.txt", "x" * (1024 * 1024))
    second = files.write("large.txt", "🙂" * 250_000, first["sha256"], offset=1024 * 1024)
    assert second["bytes"] == 1024 * 1024 + 1_000_000
    page = files.read("large.txt", offset=1024 * 1024, limit=3)
    assert page["text"] == "🙂🙂🙂" and page["has_more"]
    assert page["source_sha256"] == second["sha256"]


@pytest.mark.parametrize("offset,delete_count,insert", [
    (0, 0, "Start🙂"), (3, 4, "λ"), (9, 0, "End"), (0, 9, ""), (2, 0, "\r\n"),
])
def test_partial_edits_count_unicode_characters_across_chunk_boundaries(
    trees, monkeypatch, offset, delete_count, insert,
):
    _, files = trees
    monkeypatch.setattr(file_module, "_CHUNK_BYTES", 3)
    original = "\ufeffA🙂é\r\n界BC"
    written = files.write("unicode.txt", original)
    changed = files.write("unicode.txt", insert, written["sha256"],
                          offset=offset, delete_count=delete_count)
    expected = original[:offset] + insert + original[offset + delete_count:]
    assert (files.root / "unicode.txt").read_bytes() == expected.encode("utf-8")
    assert changed["sha256"] == sha(expected.encode("utf-8"))
    page = files.read("unicode.txt", offset=1, limit=4)
    assert page["text"] == expected[1:5]
    assert page["next_offset"] == 1 + len(expected[1:5])
    assert page["has_more"] == (page["next_offset"] < len(expected))


def test_read_above_old_32_mib_limit_streams_hash_and_unicode_page(trees):
    source, _ = trees
    target = source.root / "large.json"
    prefix = b'{"nodes":[{"id":"D","title":"D","description":"'
    suffix = 'α🙂TAIL"}],"edges":[]}'
    block = b"x" * (1024 * 1024)
    with target.open("wb") as stream:
        stream.write(prefix)
        for _ in range(33):
            stream.write(block)
        stream.write(suffix.encode("utf-8"))
    with target.open("rb") as stream:
        expected_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    offset = len(prefix) + 33 * len(block) + 1
    tracemalloc.start()
    try:
        page = source.read("large.json", offset=offset, limit=5)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page["text"] == "🙂TAIL"
    assert page["source_sha256"] == expected_sha
    assert page["file_bytes"] == target.stat().st_size > 32 * 1024 * 1024
    assert page["offset_unit"] == "Unicode code points" and page["has_more"]
    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize("arguments,match", [
    ({"offset": 0}, "require expected_sha256"),
    ({"offset": 0, "expected_sha256": "0" * 64}, "changed"),
    ({"offset": 6}, "range exceeds"),
    ({"offset": 4, "delete_count": 2}, "range exceeds"),
    ({"delete_count": 1}, "requires an offset"),
    ({"offset": True}, "Invalid offset"),
    ({"offset": 0, "delete_count": -1}, "Invalid delete count"),
])
def test_invalid_partial_edit_leaves_original_untouched(trees, arguments, match):
    _, files = trees
    written = files.write("record.txt", "A🙂BCD")
    if arguments != {"offset": 0}:
        arguments = {"expected_sha256": written["sha256"], **arguments}
    with pytest.raises(ValueError, match=match):
        files.write("record.txt", "new", **arguments)
    assert (files.root / "record.txt").read_bytes() == "A🙂BCD".encode()
    assert_no_temporary(files)


def test_stale_or_missing_hash_cannot_replace_an_existing_copy(trees):
    source, files = trees
    (source.root / "source.json").write_bytes(b"new bytes")
    existing = files.write("copy.json", "Existing content")
    for expected in (None, "0" * 64):
        with pytest.raises(ValueError):
            files.copy_from(source, "source.json", "copy.json", expected_sha256=expected)
        assert (files.root / "copy.json").read_bytes() == b"Existing content"
    copied = files.copy_from(source, "source.json", "copy.json",
                             expected_sha256=existing["sha256"])
    assert copied["sha256"] == sha(b"new bytes")


def test_stale_source_hash_leaves_copy_destination_unchanged(trees):
    source, files = trees
    (source.root / "source.json").write_bytes(b"new bytes")
    existing = files.write("copy.json", "Existing content")
    with pytest.raises(ValueError, match="changed"):
        files.copy_from(source, "source.json", "copy.json", expected_source_sha256="0" * 64,
                        expected_sha256=existing["sha256"])
    assert (files.root / "copy.json").read_bytes() == b"Existing content"
    assert_no_temporary(files)


def test_native_destination_edit_during_partial_write_is_not_overwritten(trees, monkeypatch):
    _, files = trees
    first = files.write("record.txt", "Original content")
    original = files._stream_file
    calls = 0

    def changed_during_edit(target, consume, expected_sha256=None):
        nonlocal calls
        result = original(target, consume, expected_sha256)
        calls += 1
        if calls == 2:
            target.write_bytes(b"Concurrent native content")
        return result

    monkeypatch.setattr(files, "_stream_file", changed_during_edit)
    with pytest.raises(ValueError, match="changed"):
        files.write("record.txt", "Replaced", first["sha256"], offset=0, delete_count=8)
    assert (files.root / "record.txt").read_bytes() == b"Concurrent native content"
    assert_no_temporary(files)


def test_source_change_during_copy_is_rejected(trees, monkeypatch):
    source, files = trees
    target = source.root / "source.txt"
    target.write_bytes(b"x" * 100)
    existing = files.write("copy.txt", "Keep this")
    original = source._stream_file

    def changing_read(path, consume, expected_sha256=None):
        changed = False

        def change(body):
            nonlocal changed
            consume(body)
            if not changed:
                changed = True
                target.write_bytes(b"Changed source")

        return original(path, change, expected_sha256)

    monkeypatch.setattr(source, "_stream_file", changing_read)
    with pytest.raises(ValueError, match="changed"):
        files.copy_from(source, "source.txt", "copy.txt", expected_sha256=existing["sha256"])
    assert (files.root / "copy.txt").read_bytes() == b"Keep this"
    assert_no_temporary(files)


def test_a_new_destination_appearing_during_copy_is_preserved(trees, monkeypatch):
    source, files = trees
    (source.root / "source.txt").write_bytes(b"Copy this")
    original = source._stream_file

    def appeared(path, consume, expected_sha256=None):
        result = original(path, consume, expected_sha256)
        (files.root / "new.txt").write_bytes(b"Created concurrently")
        return result

    monkeypatch.setattr(source, "_stream_file", appeared)
    with pytest.raises(FileExistsError):
        files.copy_from(source, "source.txt", "new.txt")
    assert (files.root / "new.txt").read_bytes() == b"Created concurrently"
    assert_no_temporary(files)


def test_failed_atomic_replacement_does_not_leave_a_partial_file(trees, monkeypatch):
    _, files = trees
    existing = files.write("record.txt", "Original content")

    def fail(*_args):
        raise OSError("Simulated replacement failure")

    monkeypatch.setattr(file_module.os, "replace", fail)
    with pytest.raises(OSError, match="Simulated"):
        files.write("record.txt", "Replacement", existing["sha256"], offset=0, delete_count=8)
    assert (files.root / "record.txt").read_bytes() == b"Original content"
    assert_no_temporary(files)


def test_invalid_utf8_and_oversized_insert_do_not_change_existing_file(trees):
    _, files = trees
    invalid = b"invalid\xff"
    (files.root / "invalid.txt").write_bytes(invalid)
    with pytest.raises(UnicodeDecodeError):
        files.write("invalid.txt", "new", sha(invalid), offset=0)
    assert (files.root / "invalid.txt").read_bytes() == invalid
    with pytest.raises(ValueError, match="UTF-8"):
        files.read("invalid.txt")
    with pytest.raises(ValueError, match="1 MiB"):
        files.write("invalid.txt", "x" * (1024 * 1024 + 1), sha(invalid), offset=0)
    assert (files.root / "invalid.txt").read_bytes() == invalid
    assert_no_temporary(files)


def test_native_byte_limit_bounds_read_copy_and_partial_edit(trees, monkeypatch):
    source, files = trees
    (source.root / "large.txt").write_bytes(b"x" * 11)
    existing = files.write("small.txt", "12345")
    monkeypatch.setattr(file_module, "MAX_SNAPSHOT_BYTES", 10)
    with pytest.raises(ValueError, match="byte limit"):
        source.read("large.txt")
    with pytest.raises(ValueError, match="byte limit"):
        files.copy_from(source, "large.txt", "copy.txt")
    assert not (files.root / "copy.txt").exists()
    with pytest.raises(ValueError, match="byte limit"):
        files.write("small.txt", "123456", existing["sha256"], offset=5)
    assert (files.root / "small.txt").read_bytes() == b"12345"
    assert_no_temporary(files)


def test_parallel_scoped_writers_cannot_both_apply_one_old_hash(trees):
    _, files = trees
    original = files.write("shared.txt", "Original")
    ready = threading.Barrier(2)

    def edit(text):
        ready.wait()
        try:
            return ScopedFiles(files.root).write("shared.txt", text, original["sha256"], offset=0)
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(edit, ["First", "Second"]))
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert sum(isinstance(value, ValueError) for value in outcomes) == 1
    assert (files.root / "shared.txt").read_bytes() in {b"FirstOriginal", b"SecondOriginal"}
    assert_no_temporary(files)


@pytest.mark.parametrize("path", ["../escape.txt", "/escape.txt", "file.txt:stream"])
def test_copy_and_partial_write_cannot_escape_declared_roots(trees, path):
    source, files = trees
    (source.root / "source.txt").write_bytes(b"content")
    with pytest.raises(ValueError):
        files.copy_from(source, "source.txt", path)
    with pytest.raises(ValueError):
        files.copy_from(source, path, "copy.txt")
    with pytest.raises(ValueError):
        files.write(path, "new", "0" * 64, offset=0)
    assert not (files.root / "copy.txt").exists()


def test_copy_rejects_source_and_destination_symlinks(trees, tmp_path):
    source, files = trees
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Outside content")
    try:
        (source.root / "linked.txt").symlink_to(outside)
        (files.root / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic link creation is unavailable on this host")
    (source.root / "safe.txt").write_bytes(b"Inside content")
    with pytest.raises(ValueError, match="links"):
        files.copy_from(source, "linked.txt", "copy.txt")
    with pytest.raises(ValueError, match="links"):
        files.copy_from(source, "safe.txt", "linked.txt")
    assert outside.read_bytes() == b"Outside content"
    assert_no_temporary(files)
