from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from formal_geyser.coordination.git_transport import (
    GitSyncError,
    GitSyncTransport,
    RemoteHeadMoved,
    normalize_remote,
    validate_branch,
)
from formal_geyser.coordination.repository import RepositoryError, RepositoryStore
from formal_geyser.coordination.storage import BlobStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


def git(*arguments: str, cwd: Path | None = None) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_TERMINAL_PROMPT": "0"})
    return subprocess.run(
        ["git", *arguments], check=True, capture_output=True, cwd=cwd, env=environment,
        timeout=15,
    ).stdout


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    git("init", "--bare", "--template=", "--initial-branch=main", str(path))
    return path


@pytest.fixture
def transport(tmp_path, remote):
    result = GitSyncTransport(tmp_path / "state", str(remote))
    result.initialize()
    return result


def manifest(blobs: BlobStore, files: dict[str, bytes]) -> str:
    return blobs.put_json({"files": [
        {"path": name, "sha256": blobs.put(body)} for name, body in sorted(files.items())
    ]})


class Project:
    """A managed review repository, whose published commits are what a sync may publish."""

    def __init__(self, tmp_path: Path, files: dict[str, bytes] | None = None):
        self.repository = RepositoryStore(tmp_path / "managed")
        self.blobs = BlobStore(tmp_path / "managed" / "blobs")
        self.base = self.repository.initialize(
            self.blobs, manifest(self.blobs, files or {"source.md": b"Base.\n"}))["head"]


async def publish(transport, project, state, expected_head=None, *, source=None, message="Publish\n"):
    async def prepared(commit):
        pass
    return await transport.publish_project(project.repository, source or project.base, "main", state,
                                           expected_head, message, prepared)


async def push_external(transport, remote, files, parent=None, branch="main") -> str:
    """Someone else's commit on the remote, made with test plumbing rather than the API."""
    if parent is not None:
        await transport.fetch(branch)
    cache = transport._cache
    entries = {name: ("100644", cache._hash_bytes(body, cache.path)) for name, body in files.items()}
    commit = cache._commit(cache.path, cache._tree(entries, cache.path), parent, "External edit\n")
    git("--git-dir", str(cache.path), "push", str(remote), f"{commit}:refs/heads/{branch}")
    return commit


async def test_a_publication_has_exact_bytes_and_is_unchanged_after_a_restart(transport, tmp_path, remote):
    project = Project(tmp_path, {"source.md": b"one\r\ntwo\n"})
    state = {"unicode/naïve.md": b"one\r\ntwo\n", "binary.dat": b"\x00\xff\n"}
    assert await transport.fetch("main") is None
    first = await publish(transport, project, state)
    assert first["changed"] is True
    assert transport.files(first["head"]) == {"source.md": b"one\r\ntwo\n",
                                               **{f"taskhub/{name}": body for name, body in state.items()}}
    assert await transport.fetch("main") == first["head"]
    restarted = GitSyncTransport(tmp_path / "state", str(remote))
    restarted.initialize()
    assert await publish(restarted, project, state) == {"head": first["head"], "changed": False}
    assert git("--git-dir", str(remote), "show", "-s", "--format=%P", first["head"]).strip() == project.base.encode()
    assert git("--git-dir", str(transport.path), "for-each-ref", "refs/heads", "refs/tags") == b""
    assert not (transport.path / "FETCH_HEAD").exists()
    assert not (tmp_path / "state" / "repository.git").exists()
    assert project.repository.head() == project.base  # The review repository is never changed.


async def test_a_changed_export_is_one_child_with_exact_deletions(transport, tmp_path, remote):
    project = Project(tmp_path)
    first = await publish(transport, project, {"remove.md": b"old", "keep.md": b"a"})
    second = await publish(transport, project, {"keep.md": b"b"}, first["head"])
    assert second["changed"]
    assert transport.files(second["head"]) == {"source.md": b"Base.\n", "taskhub/keep.md": b"b"}
    assert git("--git-dir", str(remote), "show", "-s", "--format=%P", second["head"]).strip() == first["head"].encode()
    assert await publish(transport, project, {"keep.md": b"b"}, first["head"]) == {
        "head": second["head"], "changed": False,
    }


async def test_an_independent_remote_edit_is_never_replaced(transport, remote, tmp_path):
    project = Project(tmp_path)
    initial = await publish(transport, project, {"state.md": b"initial"})
    external = GitSyncTransport(tmp_path / "external", str(remote))
    external.initialize()
    edited = await push_external(external, remote, {"state.md": b"operator edit"}, parent=initial["head"])
    for state in ({"state.md": b"overwrite"}, {"state.md": b"operator edit"}):
        with pytest.raises(RemoteHeadMoved):
            await publish(transport, project, state, initial["head"])
    assert await transport.fetch("main") == edited


async def test_the_push_lease_rejects_a_change_after_the_fetch(transport, remote, tmp_path, monkeypatch):
    project = Project(tmp_path)
    initial = await publish(transport, project, {"state.md": b"initial"})
    external = GitSyncTransport(tmp_path / "external", str(remote))
    external.initialize()
    original = transport._remote_git
    edited = None

    async def race(arguments, **kwargs):
        nonlocal edited
        if arguments[0] == "push" and edited is None:
            edited = await push_external(external, remote, {"state.md": b"race"}, parent=initial["head"])
        return await original(arguments, **kwargs)

    monkeypatch.setattr(transport, "_remote_git", race)
    with pytest.raises(RemoteHeadMoved):
        await publish(transport, project, {"state.md": b"export"}, initial["head"])
    assert edited is not None
    assert await transport.fetch("main") == edited
    monkeypatch.setattr(transport, "_remote_git", original)
    with pytest.raises(RemoteHeadMoved):
        await publish(transport, project, {"state.md": b"export"}, initial["head"])


async def test_a_lost_push_reply_is_reconciled_after_a_restart(transport, remote, tmp_path, monkeypatch):
    project = Project(tmp_path)
    original = transport._remote_git

    async def lost(arguments, **kwargs):
        result = await original(arguments, **kwargs)
        if arguments[0] == "push":
            raise GitSyncError("Simulated lost acknowledgement")
        return result

    monkeypatch.setattr(transport, "_remote_git", lost)
    first = await publish(transport, project, {"payload.md": b"export"})
    assert first["changed"] is False and await transport.fetch("main") == first["head"]
    restarted = GitSyncTransport(tmp_path / "state", str(remote))
    restarted.initialize()
    assert await publish(restarted, project, {"payload.md": b"export"}) == first
    assert git("--git-dir", str(remote), "rev-list", "--count", "main").strip() == b"2"  # The source and one export.


async def test_two_transport_instances_share_cache_without_blocking_the_loop(transport, remote, tmp_path):
    project = Project(tmp_path)
    second = GitSyncTransport(tmp_path / "state", str(remote))
    second.initialize()
    results = await asyncio.wait_for(asyncio.gather(
        publish(transport, project, {"payload.md": b"same export"}),
        publish(second, project, {"payload.md": b"same export"}),
    ), timeout=30)
    assert results[0]["head"] == results[1]["head"]
    assert sorted(result["changed"] for result in results) == [False, True]


async def test_only_a_published_review_commit_is_synced(transport, tmp_path):
    project = Project(tmp_path)
    workspace = tmp_path / "reviewer" / "work" / ".taskhub-integration" / "unpublished"
    prepared = project.repository.prepare(project.blobs, manifest(project.blobs, {"source.md": b"candidate"}),
                                          project.base, workspace, ["source.md"])
    with pytest.raises(GitSyncError, match="published"):
        await publish(transport, project, {}, source=prepared["candidate_commit"])
    assert await transport.fetch("main") is None
    assert project.repository.head() == project.base


@pytest.mark.parametrize("path", ["../escape", ".git/config", ".gitmodules", ".gitattributes",
                                  "nul.txt", "a\\b", "a//b", "a:stream", "C:/outside", "a."])
async def test_publish_rejects_unsafe_paths_without_contacting_remote(transport, tmp_path, path, monkeypatch):
    project = Project(tmp_path)

    async def unexpected(*args, **kwargs):
        raise AssertionError("Unsafe source caused a remote operation")
    monkeypatch.setattr(transport, "_remote_git", unexpected)
    with pytest.raises(RepositoryError):
        await publish(transport, project, {path: b"unsafe"})


@pytest.mark.parametrize("files", [{"File": b"a", "file": b"b"}, {"a": b"a", "a/b": b"b"},
                                   {"Directory/a": b"a", "directory/b": b"b"}])
async def test_publish_rejects_file_and_directory_collisions(transport, tmp_path, files):
    with pytest.raises(RepositoryError):
        await publish(transport, Project(tmp_path), files)


@pytest.mark.parametrize("mode,kind", [("120000", "blob"), ("160000", "commit")])
async def test_files_rejects_symlinks_and_submodules_in_remote(transport, remote, mode, kind):
    cache = transport._cache
    content = cache._hash_bytes(b"../outside", cache.path)
    if kind == "commit":
        content = cache._commit(cache.path, cache._tree({}, cache.path), None, "submodule")
    tree = cache._oid(cache._git(["mktree"], git_dir=cache.path,
                                 body=f"{mode} {kind} {content}\tunsafe\n".encode()).decode().strip())
    commit = cache._commit(cache.path, tree, None, "unsafe")
    # Publish the synthetic unsafe tree with test plumbing, never through the API.
    git("--git-dir", str(cache.path), "push", str(remote), f"{commit}:refs/heads/contributor")
    assert await transport.fetch("contributor") == commit
    with pytest.raises(RepositoryError, match="Symlinks"):
        transport.files(commit)


async def test_files_rejects_reserved_remote_file_instead_of_excluding_it(transport, remote):
    cache = transport._cache
    content = cache._hash_bytes(b"* filter=unsafe\n", cache.path)
    tree = cache._oid(cache._git(["mktree"], git_dir=cache.path,
                                 body=f"100644 blob {content}\t.gitattributes\n".encode()).decode().strip())
    commit = cache._commit(cache.path, tree, None, "unsafe")
    git("--git-dir", str(cache.path), "push", str(remote), f"{commit}:refs/heads/contributor")
    assert await transport.fetch("contributor") == commit
    with pytest.raises(RepositoryError):
        transport.files(commit)


@pytest.mark.parametrize("value", [
    "https://token@github.com/owner/repo.git", "https://user:token@github.com/owner/repo.git",
    "https://github.com/owner/repo?token=secret", "https://github.com/owner/repo#fragment",
    "https://github.com.evil.test/owner/repo", "http://github.com/owner/repo", "file:///tmp/remote.git",
    "ext::echo secret", "-u", "../remote.git", "./remote.git", "remote.git", "owner/repo/extra",
    "ssh://root@github.com/owner/repo", "ssh://git:token@github.com/owner/repo",
    "git@evil.test:owner/repo", "owner/re%70o", " owner/repo", "owner/repo\n",
])
def test_rejects_ambiguous_and_credential_bearing_remotes(value):
    with pytest.raises(GitSyncError):
        normalize_remote(value)


@pytest.mark.parametrize("value,expected", [
    ("owner/repo", "https://github.com/owner/repo.git"),
    ("owner/repo.git", "https://github.com/owner/repo.git"),
    ("https://github.com/owner/repo", "https://github.com/owner/repo.git"),
    ("git@github.com:owner/repo.git", "git@github.com:owner/repo.git"),
    ("ssh://git@github.com:22/owner/repo", "git@github.com:owner/repo.git"),
])
def test_normalizes_supported_github_remote_syntax(value, expected):
    assert normalize_remote(value) == expected


@pytest.mark.parametrize("value", ["--all", "refs:other", "main~1", "main.lock", "a/.b", "a//b", "a..b"])
def test_rejects_branch_refspecs_and_revision_expressions(value):
    with pytest.raises(GitSyncError):
        validate_branch(value)


def test_cache_ownership_and_tamper_checks(transport, remote, tmp_path):
    other = tmp_path / "other.git"
    git("init", "--bare", "--template=", str(other))
    with pytest.raises(GitSyncError, match="another remote"):
        GitSyncTransport(tmp_path / "state", str(other)).initialize()
    (transport.path / "config").write_bytes(b"[core]\n bare = true\n")
    with pytest.raises(RepositoryError, match="configuration"):
        transport.initialize()
    assert normalize_remote(str(remote)) == str(remote)


async def test_remote_hooks_and_unexpected_config_are_rejected(transport, remote, tmp_path):
    marker = tmp_path / "should-not-execute"
    hooks = remote / "hooks"
    hooks.mkdir()
    (hooks / "pre-receive").write_text(f"#!/bin/sh\necho unsafe > '{marker.as_posix()}'\n")
    with pytest.raises(GitSyncError, match="hooks"):
        await transport.fetch("main")
    assert not marker.exists()
    (hooks / "pre-receive").unlink()
    with (remote / "config").open("a") as stream:
        stream.write("[include]\n path = /unexpected/config\n")
    with pytest.raises(GitSyncError, match="plain bare"):
        await transport.fetch("main")


def test_remote_symlink_and_unowned_cache_are_rejected(tmp_path, remote):
    link = tmp_path / "linked.git"
    try:
        link.symlink_to(remote, target_is_directory=True)
    except OSError:
        pass
    else:
        with pytest.raises(RepositoryError, match="Linked"):
            GitSyncTransport(tmp_path / "state", str(link))
    unowned = tmp_path / "state" / "sync-cache" / "repository.git"
    unowned.mkdir(parents=True)
    with pytest.raises(GitSyncError, match="unowned"):
        GitSyncTransport(tmp_path / "state", str(remote)).initialize()


async def test_auth_commands_are_isolated_and_credentials_are_never_cached(tmp_path, monkeypatch):
    transport = GitSyncTransport(tmp_path / "state", "owner/repo", credential_helper="manager")
    transport.initialize()
    secret = "synthetic-do-not-persist"
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "http.extraHeader=Authorization: " + secret)
    monkeypatch.setenv("GIT_SSH_COMMAND", "malicious command")
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    calls = []

    async def record(command, *, environment, **kwargs):
        calls.append((command, environment))
        return 2, b""

    monkeypatch.setattr(transport, "_run", record)
    assert await transport.fetch("main") is None
    command, environment = calls[0]
    assert "credential.helper=" in command and "credential.helper=manager" in command
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_CONFIG_PARAMETERS" not in environment and "GITHUB_TOKEN" not in environment
    assert "GIT_SSH_COMMAND" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"
    assert not Path(environment["HOME"]).is_relative_to(transport._cache.state_dir)
    assert not Path(environment["XDG_CONFIG_HOME"]).is_relative_to(transport._cache.state_dir)
    assert secret not in repr(calls)
    for path in transport._cache.state_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
            assert b"credential.helper" not in path.read_bytes()


async def test_existing_helper_is_read_but_ambient_transport_config_is_not_used(tmp_path, monkeypatch):
    transport = GitSyncTransport(tmp_path / "state", "owner/repo")
    transport.initialize()
    calls = []

    async def record(command, *, environment, **kwargs):
        calls.append((command, environment))
        if "config" in command:
            return 0, b"manager\0"
        return 2, b""

    monkeypatch.setattr(transport, "_run", record)
    assert await transport.fetch("main") is None
    assert len(calls) == 2
    assert calls[0][0][-3:] == ["--null", "--get-all", "credential.helper"]
    assert calls[0][0][1:3] == ["--git-dir", str(transport.path)]
    assert "credential.helper=manager" in calls[1][0]
    assert calls[1][1]["GIT_CONFIG_GLOBAL"] == os.devnull


async def test_ssh_uses_the_existing_agent_with_prompts_and_config_commands_disabled(tmp_path, monkeypatch):
    if shutil.which("ssh") is None:
        pytest.skip("OpenSSH is required")
    transport = GitSyncTransport(tmp_path / "state", "git@github.com:owner/repo.git")
    transport.initialize()
    monkeypatch.setenv("SSH_AUTH_SOCK", "synthetic-existing-agent")
    monkeypatch.setenv("GIT_SSH_COMMAND", "must not run")
    environments = []

    async def record(command, *, environment, **kwargs):
        environments.append(environment)
        return 2, b""

    monkeypatch.setattr(transport, "_run", record)
    assert await transport.fetch("main") is None
    environment = environments[0]
    assert environment["SSH_AUTH_SOCK"] == "synthetic-existing-agent"
    assert "BatchMode=yes" in environment["GIT_SSH_COMMAND"]
    assert "StrictHostKeyChecking=yes" in environment["GIT_SSH_COMMAND"]
    assert "empty-ssh-config" in environment["GIT_SSH_COMMAND"]
    assert "must not run" not in environment["GIT_SSH_COMMAND"]


@pytest.mark.parametrize("helper", ["!echo token", "manager --token=secret", "a;b", "a\nb", "-helper"])
def test_credential_helpers_cannot_be_shell_commands(tmp_path, helper):
    with pytest.raises(GitSyncError, match="native Git helper"):
        GitSyncTransport(tmp_path, "owner/repo", credential_helper=helper)


async def test_failures_never_echo_credential_helper_diagnostics(transport):
    secret = "synthetic-token-in-stderr"
    with pytest.raises(GitSyncError) as error:
        await transport._run(
            [sys.executable, "-c", f"import sys; print('{secret}',file=sys.stderr); sys.exit(1)"],
            environment=transport._environment(),
        )
    assert secret not in str(error.value)


async def test_timeout_stops_descendant_before_it_can_write(transport, tmp_path):
    marker = tmp_path / "child-survived"
    code = f"import time; from pathlib import Path; time.sleep(1.2); Path({str(marker)!r}).write_text('bad')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{code!r}]); time.sleep(5)"
    transport.timeout_seconds = 0.5
    with pytest.raises(GitSyncError, match="timed out"):
        await transport._run([sys.executable, "-c", parent], environment=transport._environment())
    await asyncio.sleep(1.3)
    assert not marker.exists()


async def test_remote_output_is_bounded(transport):
    with pytest.raises(GitSyncError, match="output limit"):
        await transport._run([sys.executable, "-c", "print('x'*10000)"],
                             environment=transport._environment(), limit=128)
