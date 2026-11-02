from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from formal_geyser.coordination.repository import (
    HeadMoved,
    RepositoryError,
    RepositoryStore,
    UnresolvedConflicts,
)
from formal_geyser.coordination.storage import BlobStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


def manifest(blobs: BlobStore, files: dict[str, bytes]) -> str:
    return blobs.put_json({"files": [
        {"path": name, "sha256": blobs.put(body)} for name, body in sorted(files.items())
    ]})


def contents(blobs: BlobStore, snapshot: str) -> dict[str, bytes]:
    return {entry["path"]: blobs.get(entry["sha256"])
            for entry in blobs.get_json(snapshot)["files"]}


@pytest.fixture
def store(tmp_path):
    blobs = BlobStore(tmp_path / "state" / "blobs")
    return RepositoryStore(tmp_path / "state"), blobs


def workspace(tmp_path: Path, name: str) -> Path:
    return tmp_path / "reviewer" / "work" / ".taskhub-integration" / name


def freeze(store, tmp_path, name, files, base, paths=None):
    repository, blobs = store
    root = workspace(tmp_path, name)
    repository.prepare(blobs, manifest(blobs, files), base, root,
                       list(files) if paths is None else paths)
    return repository.finalize(root, blobs)


def test_empty_initialization_and_restart_are_idempotent(store):
    repository, blobs = store
    initial = repository.initialize(blobs)
    assert initial == {"head": repository.head(), "branch": "main"}
    assert contents(blobs, repository.snapshot(initial["head"], blobs)) == {}
    restarted = RepositoryStore(repository.state_dir)
    assert restarted.initialize(blobs, manifest(blobs, {"ignored": b"second seed"})) == initial


def test_initial_snapshot_and_freeze_preserve_exact_bytes_despite_host_config(store, tmp_path, monkeypatch):
    repository, blobs = store
    hook_output = tmp_path / "hook-executed"
    hooks = tmp_path / "host-hooks"
    hooks.mkdir()
    (hooks / "post-merge").write_text(f"#!/bin/sh\nprintf unsafe > '{hook_output.as_posix()}'\n")
    host_config = tmp_path / "host-config"
    host_config.write_text(
        f"[core]\n autocrlf = true\n hooksPath = {hooks.as_posix()}\n"
        "[include]\n path = /this/must/not/be/read\n", encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(host_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated"))
    original = {"unicode/naïve.txt": b"first\r\nsecond\n", "binary.dat": b"\x00\xff\r\n"}
    initial = repository.initialize(blobs, manifest(blobs, original))["head"]
    assert contents(blobs, repository.snapshot(initial, blobs)) == original
    final = freeze(store, tmp_path, "bytes", {"binary.dat": b"\x00\x81\n\r\n"}, initial)
    assert contents(blobs, final["snapshot"]) == {
        "unicode/naïve.txt": original["unicode/naïve.txt"], "binary.dat": b"\x00\x81\n\r\n",
    }
    assert repository.head() == initial
    assert not hook_output.exists()


def test_three_way_merge_preserves_public_edits_and_selected_scope(store, tmp_path):
    repository, blobs = store
    base_files = {"theorem.txt": b"one\ntwo\nthree\nfour\nfive\n", "caller.txt": b"old caller\n"}
    base = repository.initialize(blobs, manifest(blobs, base_files))["head"]
    public = freeze(store, tmp_path, "public", {
        "theorem.txt": b"one\ntwo\nthree\nfour\npublic five\n", "public.txt": b"independent\n",
    }, base)
    repository.publish(public["commit"], base)
    work = workspace(tmp_path, "review")
    work.parent.parent.mkdir(parents=True, exist_ok=True)
    preserved = work.parent.parent / "private-reviewer-notes.txt"
    preserved.write_bytes(b"must stay")
    candidate_files = {
        "theorem.txt": b"candidate one\ntwo\nthree\nfour\nfive\n",
        "caller.txt": b"unselected author edit\n", "private.txt": b"not selected\n",
    }
    prepared = repository.prepare(blobs, manifest(blobs, candidate_files), base, work, ["theorem.txt"])
    assert prepared["conflicts"] == []
    assert prepared["target_head"] == public["commit"]
    assert (work / "theorem.txt").read_bytes() == b"candidate one\ntwo\nthree\nfour\npublic five\n"
    assert (work / "caller.txt").read_bytes() == b"old caller\n"
    assert (work / "public.txt").read_bytes() == b"independent\n"
    assert not (work / "private.txt").exists()
    (work / "caller.txt").write_bytes(b"reviewer repaired caller\n")
    (work / "new-helper.txt").write_bytes(b"reviewer helper\n")
    (work / "build-output.tmp").write_bytes(b"untracked build output")
    repository.resolve(work, ["new-helper.txt"])
    status = repository.status(work)
    assert "caller.txt" in status["changed_paths"]
    assert status["untracked"] == ["build-output.tmp"]
    final = repository.finalize(work, blobs)
    assert final["target_head"] == public["commit"]
    assert final["changed_paths"] == ["caller.txt", "new-helper.txt", "theorem.txt"]
    assert contents(blobs, final["snapshot"]) == {
        "theorem.txt": b"candidate one\ntwo\nthree\nfour\npublic five\n",
        "caller.txt": b"reviewer repaired caller\n", "new-helper.txt": b"reviewer helper\n",
        "public.txt": b"independent\n",
    }
    assert repository.head() == public["commit"]
    assert preserved.read_bytes() == b"must stay"
    assert repository.publish(final["commit"], public["commit"])["published"]
    assert contents(blobs, repository.snapshot(repository.head(), blobs)) == contents(blobs, final["snapshot"])


@pytest.mark.parametrize("initial,public,candidate", [
    ({"file.txt": b"base\n"}, {"file.txt": b"public\n"}, {"file.txt": b"candidate\n"}),
    ({}, {"file.txt": b"public addition\n"}, {"file.txt": b"candidate addition\n"}),
    ({"file.txt": b"base\n"}, {"file.txt": b"public modification\n"}, {}),
    ({"file.txt": b"base\n"}, {}, {"file.txt": b"candidate modification\n"}),
])
def test_real_text_add_and_delete_conflicts_require_explicit_resolution(
        store, tmp_path, initial, public, candidate):
    repository, blobs = store
    base = repository.initialize(blobs, manifest(blobs, initial))["head"]
    advanced = freeze(store, tmp_path, "public", public, base, ["file.txt"])
    repository.publish(advanced["commit"], base)
    work = workspace(tmp_path, "conflicted")
    prepared = repository.prepare(blobs, manifest(blobs, candidate), base, work, ["file.txt"])
    assert prepared["conflicts"] == ["file.txt"]
    assert repository.head() == advanced["commit"]
    with pytest.raises(UnresolvedConflicts):
        repository.finalize(work, blobs)
    (work / "file.txt").write_bytes(b"<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n")
    with pytest.raises(UnresolvedConflicts, match="markers"):
        repository.resolve(work, ["file.txt"])
    (work / "file.txt").write_bytes(b"reviewed resolution\n")
    assert repository.resolve(work, ["file.txt"])["conflicts"] == []
    final = repository.finalize(work, blobs)
    assert contents(blobs, final["snapshot"]) == {"file.txt": b"reviewed resolution\n"}
    assert repository.head() == advanced["commit"]
    assert repository.publish(final["commit"], advanced["commit"])["published"]


def test_conflict_can_be_resolved_by_deleting_file(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs, manifest(blobs, {"file": b"base"}))["head"]
    advanced = freeze(store, tmp_path, "public", {"file": b"public"}, base)
    repository.publish(advanced["commit"], base)
    work = workspace(tmp_path, "delete")
    repository.prepare(blobs, manifest(blobs, {}), base, work, ["file"])
    (work / "file").unlink()
    assert repository.resolve(work, ["file"])["conflicts"] == []
    final = repository.finalize(work, blobs)
    assert contents(blobs, final["snapshot"]) == {}
    assert final["changed_paths"] == ["file"]


def test_freeze_can_be_repeated_after_further_review_edits(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    candidate = manifest(blobs, {"proof": b"first"})
    work = workspace(tmp_path, "repeat")
    prepared = repository.prepare(blobs, candidate, base, work, ["proof"])
    first = repository.finalize(work, blobs)
    assert repository.finalize(work, blobs) == first
    restarted = RepositoryStore(repository.state_dir)
    assert restarted.prepare(blobs, candidate, base, work, ["proof"]) == prepared
    (work / "proof").write_bytes(b"second review")
    second = restarted.finalize(work, blobs)
    assert second["commit"] != first["commit"]
    assert second["target_head"] == first["target_head"] == base
    assert contents(blobs, repository.snapshot(first["commit"], blobs)) == {"proof": b"first"}
    assert contents(blobs, second["snapshot"]) == {"proof": b"second review"}
    assert repository.head() == base
    assert repository.publish(second["commit"], base)["published"]


def test_publication_compare_and_swap_and_exact_retry_survive_restart(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    first = freeze(store, tmp_path, "first", {"first": b"first"}, base)
    second = freeze(store, tmp_path, "second", {"second": b"second"}, base)
    assert repository.publish(first["commit"], base) == {
        "head": first["commit"], "branch": "main", "published": True,
    }
    restarted = RepositoryStore(repository.state_dir)
    assert restarted.publish(first["commit"], base)["published"] is False
    with pytest.raises(HeadMoved):
        restarted.publish(second["commit"], base)
    assert restarted.head() == first["commit"]
    with pytest.raises(RepositoryError, match="frozen"):
        restarted.publish(first["commit"], second["commit"])
    with pytest.raises(RepositoryError):
        restarted.publish(base, base)


@pytest.mark.parametrize("path", [
    "../escape", "/absolute", "C:/outside", "a/../../escape", "a\\b", "a//b", "a/./b",
    ".git/config", "nested/.GIT/config", ".gitmodules", "nested/.gitattributes", ".gitconfig",
    ".private/notes.md", "nested/.PRIVATE/notes.md",
    ".git./config", "GIT~1/config", ".git~1/config", "file:stream", "nul.txt", "COM1", "lpt².txt",
    "trailing.", "trailing ", "line\nbreak", ".taskhub-integration/session/data", "CONIN$", ".gıt/config",
])
def test_snapshot_rejects_unsafe_paths(store, path):
    repository, blobs = store
    with pytest.raises(RepositoryError):
        repository.initialize(blobs, manifest(blobs, {path: b"unsafe"}))


@pytest.mark.parametrize("names", [
    ["File", "file"], ["Directory/a", "directory/b"], ["a", "a/b"], ["dotless_ı", "dotless_I"],
])
def test_snapshot_rejects_case_and_file_directory_collisions(store, names):
    repository, blobs = store
    with pytest.raises(RepositoryError):
        repository.initialize(blobs, manifest(blobs, {name: b"x" for name in names}))


def test_selected_overlay_rejects_collision_with_existing_public_path(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs, manifest(blobs, {"File": b"original"}))["head"]
    with pytest.raises(RepositoryError, match="case"):
        repository.prepare(blobs, manifest(blobs, {"file": b"new"}), base,
                           workspace(tmp_path, "collision"), ["file"])
    assert repository.head() == base


def test_existing_reviewer_files_and_unowned_workspace_are_preserved(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    work = workspace(tmp_path, "existing")
    work.mkdir(parents=True)
    (work / "notes").write_bytes(b"existing reviewer notes")
    with pytest.raises(RepositoryError, match="overwrite"):
        repository.prepare(blobs, manifest(blobs, {}), base, work, [])
    assert (work / "notes").read_bytes() == b"existing reviewer notes"
    with pytest.raises(RepositoryError, match=".taskhub-integration"):
        repository.prepare(blobs, manifest(blobs, {}), base, tmp_path / "ordinary-work", [])
    with pytest.raises(RepositoryError):
        repository.finalize(work, blobs)


@pytest.mark.parametrize("relative,body", [
    ("config", b"[core]\n bare = true\n"),
    ("hooks/pre-commit", b"#!/bin/sh\nexit 99\n"),
    ("info/attributes", b"* filter=surprise\n"),
    ("objects/info/alternates", b"/not-the-managed-repository/objects\n"),
    ("HEAD", b"ref: refs/heads/forged\n"),
])
def test_modified_owned_workspace_metadata_is_rejected(store, tmp_path, relative, body):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    work = workspace(tmp_path, "metadata")
    repository.prepare(blobs, manifest(blobs, {"file": b"safe"}), base, work, ["file"])
    target = work / ".git" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    with pytest.raises(RepositoryError):
        repository.finalize(work, blobs)
    assert repository.head() == base


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_symlink_and_submodule_index_entries_are_rejected(store, tmp_path, mode):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    work = workspace(tmp_path, "special-index")
    repository.prepare(blobs, manifest(blobs, {"file": b"safe"}), base, work, ["file"])
    sha = base if mode == "160000" else repository._hash_bytes(b"../../escape", work / ".git")
    repository._git(["update-index", "--add", "--cacheinfo", f"{mode},{sha},malicious"],
                    git_dir=work / ".git", workspace=work)
    with pytest.raises(RepositoryError, match="Symlinks"):
        repository.finalize(work, blobs)
    with pytest.raises(RepositoryError, match="Symlinks"):
        repository.status(work)


def test_linked_tracked_files_are_rejected_without_following_them(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    work = workspace(tmp_path, "symlink")
    repository.prepare(blobs, manifest(blobs, {"file": b"safe"}), base, work, ["file"])
    outside = tmp_path / "outside"
    outside.write_bytes(b"private outside data")
    (work / "file").unlink()
    try:
        (work / "file").symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    with pytest.raises(RepositoryError, match="Linked"):
        repository.finalize(work, blobs)
    with pytest.raises(RepositoryError, match="Linked"):
        repository.resolve(work, ["file"])
    assert outside.read_bytes() == b"private outside data"


def test_missing_marker_files_and_forged_workspace_sessions_are_rejected(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    original = workspace(tmp_path, "original")
    repository.prepare(blobs, manifest(blobs, {"file": b"safe"}), base, original, ["file"])
    copied = workspace(tmp_path, "forged")
    shutil.copytree(original, copied)
    with pytest.raises(RepositoryError):
        repository.status(copied)
    (original / "file").write_bytes(b"<<<<<<< ours\nstill conflict markers\n")
    with pytest.raises(UnresolvedConflicts):
        repository.finalize(original, blobs)


def test_rejects_unsafe_executable_branch_and_unowned_repository(tmp_path):
    with pytest.raises(RepositoryError, match="executable"):
        RepositoryStore(tmp_path / "state", executable="a-git-executable-that-does-not-exist")
    for name in ["../main", "-main", "main.lock", "a//b", "a/.b", "main\nother"]:
        with pytest.raises(RepositoryError, match="branch"):
            RepositoryStore(tmp_path / "state", branch=name)
    state = tmp_path / "state"
    (state / "repository.git").mkdir(parents=True)
    (state / "repository.git" / "keep").write_bytes(b"preexisting")
    with pytest.raises(RepositoryError, match="unowned"):
        RepositoryStore(state).initialize(BlobStore(state / "blobs"))
    assert (state / "repository.git" / "keep").read_bytes() == b"preexisting"


def test_environment_does_not_inherit_git_or_execution_injection(store, monkeypatch):
    repository, _ = store
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/unsafe'")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/outside")
    monkeypatch.setenv("GIT_SSH_COMMAND", "untrusted")
    monkeypatch.setenv("LD_PRELOAD", "untrusted")
    environment = repository._environment()
    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert "GIT_OBJECT_DIRECTORY" not in environment
    assert "GIT_SSH_COMMAND" not in environment
    assert "LD_PRELOAD" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull


def test_markdown_setext_headings_survive_resolution_and_freeze(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    work = workspace(tmp_path, "markdown-heading")
    body = b"Reviewed explanation\r\n====================\r\nA legitimate Markdown heading.\r\n"
    repository.prepare(blobs, manifest(blobs, {"Explanation.md": body}), base, work,
                       ["Explanation.md"])
    repository.resolve(work, ["Explanation.md"])
    frozen = repository.finalize(work, blobs)
    assert contents(blobs, frozen["snapshot"]) == {"Explanation.md": body}


@pytest.mark.parametrize("body", [
    b"<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> candidate\n",
    b"<<<<<<< HEAD\nours\n||||||| base\nbase\n=======\ntheirs\n>>>>>>> candidate\n",
    b"leftover opener\n<<<<<<< HEAD\n",
    b"leftover closer\n>>>>>>> candidate\n",
])
def test_native_staging_cannot_hide_remaining_conflict_text(store, tmp_path, body):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    work = workspace(tmp_path, "staged-markers")
    repository.prepare(blobs, manifest(blobs, {"file": b"original\n"}), base, work, ["file"])
    (work / "file").write_bytes(body)
    sha = repository._hash_bytes(body, work / ".git")
    repository._git(["update-index", "--add", "--cacheinfo", f"100644,{sha},file"],
                    git_dir=work / ".git", workspace=work)
    assert repository.status(work)["conflicts"] == []
    with pytest.raises(UnresolvedConflicts, match="markers"):
        repository.finalize(work, blobs)


@pytest.mark.parametrize("operation", ["read-tree", "merge"])
def test_prepare_resumes_an_owned_interrupted_checkout_or_merge(store, tmp_path, monkeypatch, operation):
    repository, blobs = store
    base = repository.initialize(blobs, manifest(blobs, {"original": b"base"}))["head"]
    source = manifest(blobs, {"added": b"candidate"})
    work = workspace(tmp_path, "interrupted")
    original_git = repository._git
    interrupted = False

    def crash(args, **kwargs):
        nonlocal interrupted
        result = original_git(args, **kwargs)
        if args[0] == operation and kwargs.get("workspace") == work and not interrupted:
            interrupted = True
            raise RuntimeError("simulated process crash after Git completed")
        return result

    monkeypatch.setattr(repository, "_git", crash)
    with pytest.raises(RuntimeError, match="crash"):
        repository.prepare(blobs, source, base, work, ["added"])
    assert interrupted
    restarted = RepositoryStore(repository.state_dir)
    prepared = restarted.prepare(blobs, source, base, work, ["added"])
    assert prepared["conflicts"] == []
    frozen = restarted.finalize(work, blobs)
    assert contents(blobs, frozen["snapshot"]) == {"original": b"base", "added": b"candidate"}
    assert restarted.head() == base
