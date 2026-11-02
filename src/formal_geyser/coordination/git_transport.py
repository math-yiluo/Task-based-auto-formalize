"""Bounded native Git transport isolated from the local review repository.

Every push names an immutable commit. An explicit lease is only an atomic
expected-head check: ancestry is proved first, so it never permits a history
rewrite. The cache has private refs, no worktree, and no credential material.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shlex
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit
from uuid import uuid4

import portalocker

from .repository import (
    _MAX_OUTPUT,
    HeadMoved,
    RepositoryError,
    RepositoryStore,
    _paths,
    _safe_path,
)
from .runtime.process import OwnedProcessHost, ProcessHostError
from .snapshot_limits import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES
from .storage import canonical

_MAX_ERRORS = 1024 * 1024
_MAX_STATE_BYTES = 64 * 1024 * 1024
_MAX_STATE_FILES = 20_000  # Generated taskhub/ state keeps its own allowance.
_MAX_SYNC_BYTES = MAX_SNAPSHOT_BYTES + _MAX_STATE_BYTES
_MAX_SYNC_FILES = MAX_SNAPSHOT_FILES + _MAX_STATE_FILES
_MAX_CACHE_BYTES = 4 * _MAX_SYNC_BYTES
_HELPER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z")
STATE_DIRECTORY = "taskhub"


class GitSyncError(RepositoryError):
    """Invalid sync configuration or a bounded remote Git operation failure."""


class RemoteHeadMoved(HeadMoved, GitSyncError):
    """The remote branch differs from the operation's exact expected head."""


def validate_branch(value: str) -> str:
    """Accept an unambiguous branch name, never a refspec or command option."""
    if (not isinstance(value, str) or len(value) > 240
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
            or ".." in value or "//" in value or value.endswith(("/", ".", ".lock"))
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise GitSyncError("Invalid sync branch name")
    return value


def normalize_remote(value: str) -> str:
    """Accept GitHub HTTPS/SSH, OWNER/REPO, or an absolute local testing path.

    User information in HTTPS URLs, URL parameters, remote helpers, arbitrary
    hosts, and ambiguous relative filesystem paths are deliberately unsupported.
    """
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value != value.strip() or any(ord(char) < 32 for char in value)
            or value.startswith("-")):
        raise GitSyncError("Invalid Git sync remote")
    if Path(value).is_absolute():
        # Local remotes cannot escape through filesystem links or UNC shares.
        if value.startswith(("\\\\", "//")):
            raise GitSyncError("A local sync remote must be a local absolute path")
        return str(_safe_path(Path(value)))
    if any(char in value for char in "%?#\\"):
        raise GitSyncError("Git sync URLs cannot contain credentials or URL parameters")
    protocol = "https"
    name = value
    if value.startswith("git@github.com:"):
        protocol, name = "ssh", value[len("git@github.com:"):]
    elif "://" in value:
        try:
            parsed = urlsplit(value)
            if parsed.hostname != "github.com" or parsed.query or parsed.fragment:
                raise ValueError
            if parsed.scheme == "https":
                if parsed.username is not None or parsed.password is not None or parsed.port is not None:
                    raise ValueError
            elif parsed.scheme == "ssh":
                if parsed.username != "git" or parsed.password is not None or parsed.port not in (None, 22):
                    raise ValueError
                protocol = "ssh"
            else:
                raise ValueError
            name = parsed.path.removeprefix("/")
        except ValueError:
            raise GitSyncError("Only credential-free GitHub HTTPS or Git SSH URLs are supported") from None
    if name.endswith(".git"):
        name = name[:-4]
    if not _REPOSITORY.fullmatch(name) or name.split("/")[-1] in {".", ".."}:
        raise GitSyncError("Use a GitHub OWNER/REPO or an absolute local bare repository path")
    return f"https://github.com/{name}.git" if protocol == "https" else f"git@github.com:{name}.git"


class GitSyncTransport:
    """A single remote and an owned, independent cache of immutable Git objects."""

    def __init__(self, state_dir: Path, remote: str, executable: str = "git",
                 credential_helper: str | None = None, timeout_seconds: float = 60):
        self.remote = normalize_remote(remote)
        if (credential_helper is not None
                and (not isinstance(credential_helper, str) or not _HELPER.fullmatch(credential_helper))):
            raise GitSyncError("A credential helper must be a native Git helper name without arguments")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600):
            raise GitSyncError("The Git sync timeout must be greater than zero and at most 600 seconds")
        self.credential_helper = credential_helper
        self.timeout_seconds = float(timeout_seconds)
        self.state_dir = _safe_path(state_dir)
        self._cache = RepositoryStore(self.state_dir / "sync-cache", "sync-cache", executable,
                                      max_files=_MAX_SYNC_FILES, max_bytes=_MAX_SYNC_BYTES)
        self.path = self._cache.path
        self.control = self._cache.control
        self.executable = self._cache.executable
        self._mutex = asyncio.Lock()
        self._helpers: list[str] | None = None

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        # A blocking file lock in the event-loop thread would deadlock two
        # transport instances sharing a cache while the first awaits Git.
        async with self._mutex:
            lock = portalocker.Lock(str(_safe_path(self.control / "lock")), timeout=0)
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    lock.acquire()
                    break
                except portalocker.LockException:
                    if time.monotonic() >= deadline:
                        raise GitSyncError("Timed out waiting for the owned sync cache") from None
                    await asyncio.sleep(0.05)
            try:
                yield
            finally:
                lock.release()

    def initialize(self) -> None:
        """Initialize only a cache; do not contact a remote or create a public ref."""
        cache = self._cache
        with cache._locked():
            marker = self.control / "owner.json"
            if not marker.exists():
                if self.path.exists():
                    raise GitSyncError("Refusing to adopt an unowned sync cache")
                for directory in (self.control / "empty-hooks", self.control / "publications"):
                    _safe_path(directory).mkdir(exist_ok=True)
                for name in ("empty-attributes", "empty-ssh-config"):
                    target = _safe_path(self.control / name)
                    if target.exists() and target.read_bytes():
                        raise GitSyncError("Unexpected configuration in sync cache control")
                    target.write_bytes(b"")
                cache._git(["init", "--bare", "--template=", "--object-format=sha1",
                            "--initial-branch=sync-cache", str(self.path)])
                (self.path / "config").write_bytes(cache._config(True))
                cache._write_json(self.control / "remote.json", {"format": 1, "remote": self.remote})
                cache._write_json(marker, {"format": 1, "repository": str(self.path),
                                           "branch": cache.branch})
            self._owned()

    def _owned(self) -> None:
        self._cache._owned()
        if self._cache._read_json(self.control / "remote.json") != {"format": 1, "remote": self.remote}:
            raise GitSyncError("The sync cache belongs to another remote")
        if _safe_path(self.control / "empty-ssh-config").read_bytes() != b"":
            raise GitSyncError("The sync SSH configuration was modified")
        if self._cache._git(["for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"],
                            git_dir=self.path):
            raise GitSyncError("Public refs are forbidden in the sync cache")

    def _environment(self) -> dict[str, str]:
        environment = self._cache._environment()
        # Credential helpers and SSH use the operator's existing OS account.
        # No inherited GIT_*, token variables, proxies, or helper commands enter.
        for key in ("USER", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "DBUS_SESSION_BUS_ADDRESS",
                    "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            if key in os.environ:
                environment[key] = os.environ[key]
        operator_home = os.environ.get("HOME") or str(Path.home())
        environment["HOME"] = operator_home
        environment["XDG_CONFIG_HOME"] = os.environ.get(
            "XDG_CONFIG_HOME", str(Path(operator_home) / ".config"),
        )
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
                            "GCM_GUI_PROMPT": "0", "SSH_ASKPASS_REQUIRE": "never"})
        return environment

    async def _run(self, command: list[str], *, environment: dict[str, str],
                   allowed: tuple[int, ...] = (0,), limit: int = _MAX_ERRORS) -> tuple[int, bytes]:
        """Own and drain the entire Git/helper/SSH process tree without logging it."""
        host: OwnedProcessHost | None = None

        async def read(stream: asyncio.StreamReader, maximum: int) -> bytes:
            result = bytearray()
            while block := await stream.read(65536):
                result.extend(block)
                if len(result) > maximum:
                    raise GitSyncError("Git sync exceeded the output limit")
            return bytes(result)

        async def watch() -> None:
            while True:
                total = 0
                for root, directories, files in os.walk(self.path / "objects", followlinks=False):
                    for name in directories + files:
                        path = _safe_path(Path(root) / name)
                        if path.is_file():
                            try:
                                total += path.stat().st_size
                            except FileNotFoundError:
                                # Git can atomically rename a received pack during this scan.
                                continue
                            if total > _MAX_CACHE_BYTES:
                                raise GitSyncError("Git sync cache exceeds its bounded history allowance")
                await asyncio.sleep(0.05)

        readers: list[asyncio.Task[Any]] = []
        watchdog: asyncio.Task[None] | None = None
        result: asyncio.Future[list[Any]] | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                host = await OwnedProcessHost.start(
                    command, cwd=self.control, env=environment,
                    startup_timeout=min(10.0, self.timeout_seconds),
                )
                host.stdin.close()
                readers = [asyncio.create_task(read(host.stdout, limit)),
                           asyncio.create_task(read(host.stderr, _MAX_ERRORS))]
                watchdog = asyncio.create_task(watch())
                result = asyncio.gather(*readers, host.process.wait())
                done, _ = await asyncio.wait((result, watchdog), return_when=asyncio.FIRST_COMPLETED)
                if watchdog in done:
                    await watchdog
                output, _, returncode = await result
                if returncode not in allowed:
                    # Remote diagnostics and helper output may contain credentials.
                    raise GitSyncError(f"Git sync operation failed (exit {returncode}); check remote access")
                return returncode, output
        except TimeoutError:
            raise GitSyncError("Git sync operation timed out") from None
        except (OSError, ProcessHostError):
            raise GitSyncError("Unable to start or read the native Git sync process") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            if host is not None:
                cleanup = asyncio.create_task(host.stop(grace=0.1))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
            if result is not None:
                result.cancel()
                await asyncio.gather(result, return_exceptions=True)
            for reader in readers:
                reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)

    async def _credential_helpers(self) -> list[str]:
        if self._helpers is not None:
            return self._helpers
        if self.credential_helper is not None:
            self._helpers = [self.credential_helper]
            return self._helpers
        # Read only helper names from operator configuration; never inherit it for
        # network commands (it can contain URL rewrites, hooks, and shell aliases).
        environment = self._environment()
        for name in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
            environment.pop(name, None)
        if "XDG_CONFIG_HOME" in os.environ:
            environment["XDG_CONFIG_HOME"] = os.environ["XDG_CONFIG_HOME"]
        else:
            environment.pop("XDG_CONFIG_HOME", None)
        _, raw = await self._run(
            [self.executable, "--git-dir", str(self.path), "config", "--null",
             "--get-all", "credential.helper"],
            environment=environment, allowed=(0, 1), limit=8192,
        )
        helpers: list[str] = []
        for item in raw.split(b"\0")[:-1]:
            try:
                helper = item.decode("utf-8")
            except UnicodeError:
                raise GitSyncError("Configure a native credential helper name explicitly") from None
            if not helper:
                helpers.clear()
            elif not _HELPER.fullmatch(helper):
                raise GitSyncError("Configure a native credential helper name explicitly")
            else:
                helpers.append(helper)
        self._helpers = helpers
        return helpers

    def _local_remote(self) -> None:
        if not Path(self.remote).is_absolute():
            return
        remote = _safe_path(Path(self.remote))
        if not remote.is_dir():
            raise GitSyncError("The local sync remote must be an existing bare repository")
        for root, directories, files in os.walk(remote, followlinks=False):
            for name in directories + files:
                _safe_path(Path(root) / name)
        for relative in ("commondir", "gitdir", "config.worktree", "info/grafts", "info/attributes",
                         "objects/info/alternates", "objects/info/http-alternates", "shallow"):
            if (remote / relative).exists():
                raise GitSyncError("The local sync remote contains redirected Git configuration")
        hooks = remote / "hooks"
        if hooks.exists() and any(not item.name.endswith(".sample") for item in hooks.iterdir()):
            raise GitSyncError("Executable hooks are forbidden in a local sync testing remote")
        raw = self._cache._git(["config", "--file", str(remote / "config"), "--no-includes",
                               "--null", "--list"])
        settings: dict[str, str] = {}
        safe = {"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
                "core.ignorecase", "core.symlinks", "core.autocrlf", "core.precomposeunicode",
                "receive.denynonfastforwards", "receive.denydeletes"}
        try:
            for record in raw.split(b"\0"):
                if record:
                    key, value = record.decode("utf-8").split("\n", 1)
                    if key.lower() not in safe:
                        raise ValueError
                    settings[key.lower()] = value
        except (UnicodeError, ValueError):
            raise GitSyncError("Local testing remotes require a plain bare Git configuration") from None
        if settings.get("core.bare") != "true" or settings.get("core.repositoryformatversion") != "0":
            raise GitSyncError("The local sync remote must be a SHA-1 bare repository")

    async def _remote_git(self, arguments: list[str], *, allowed: tuple[int, ...] = (0,)) -> bytes:
        self._local_remote()
        environment = self._environment()
        config = [
            f"core.hooksPath={self.control / 'empty-hooks'}", "core.fsmonitor=false",
            "core.autocrlf=false", "core.symlinks=false", "core.pager=cat", "gc.auto=0",
            "maintenance.auto=false", "protocol.allow=never", "credential.helper=",
            "credential.interactive=false", "submodule.recurse=false", "fetch.recurseSubmodules=false",
            "fetch.writeCommitGraph=false", "transfer.fsckObjects=true", "fetch.fsckObjects=true",
            "push.followTags=false", "http.followRedirects=false", "http.sslVerify=true",
            "core.askPass=", "core.logAllRefUpdates=false",
        ]
        if Path(self.remote).is_absolute():
            config.append("protocol.file.allow=always")
        elif self.remote.startswith("https://"):
            config.append("protocol.https.allow=always")
            config.extend(f"credential.helper={helper}" for helper in await self._credential_helpers())
        else:
            config.append("protocol.ssh.allow=always")
            ssh = shutil.which("ssh")
            if ssh is None:
                raise GitSyncError("A native OpenSSH executable is required")
            environment["GIT_SSH_COMMAND"] = shlex.join([
                str(Path(ssh).absolute()).replace("\\", "/"), "-F",
                str(self.control / "empty-ssh-config").replace("\\", "/"),
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no",
            ])
            environment["GIT_SSH_VARIANT"] = "ssh"
        command = [self.executable, "--no-pager", "--literal-pathspecs"]
        for setting in config:
            command.extend(["-c", setting])
        command.extend(["--git-dir", str(self.path), *arguments])
        _, output = await self._run(command, environment=environment, allowed=allowed)
        return output

    async def _remote_head(self, branch: str) -> str | None:
        ref = f"refs/heads/{validate_branch(branch)}"
        raw = await self._remote_git(["ls-remote", "--refs", "--exit-code", "--", self.remote, ref],
                                     allowed=(0, 2))
        if not raw:
            return None
        try:
            records = raw.decode("ascii").splitlines()
            sha, actual = records[0].split("\t")
            if len(records) != 1 or actual != ref:
                raise ValueError
            return self._cache._oid(sha)
        except (ValueError, UnicodeError, IndexError):
            raise GitSyncError("The remote returned an invalid branch advertisement") from None

    async def _fetch(self, branch: str) -> str | None:
        branch = validate_branch(branch)
        if await self._remote_head(branch) is None:
            return None
        private = f"refs/taskhub-sync/fetches/{uuid4().hex}"
        try:
            await self._remote_git([
                "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                "--no-auto-maintenance", "--", self.remote, f"refs/heads/{branch}:{private}",
            ])
            commit = self._cache._oid(self._cache._git(
                ["rev-parse", "--verify", private], git_dir=self.path,
            ).decode("ascii").strip())
            self._cache._commit_tree(self.path, commit)
            self._cache._git(["update-ref", f"refs/taskhub-sync/objects/{commit}", commit],
                             git_dir=self.path)
            return commit
        finally:
            self._cache._git(["update-ref", "-d", private], git_dir=self.path)

    async def fetch(self, branch: str) -> str | None:
        """Fetch exactly one remote branch into private cache refs only."""
        async with self._locked():
            with self._cache._mutex:
                self._owned()
                return await self._fetch(branch)

    def files(self, commit: str) -> dict[str, bytes]:
        """Read every regular source file; reject unsafe entries instead of omitting them."""
        with self._cache._locked():
            self._owned()
            tree = self._cache._commit_tree(self.path, self._cache._oid(commit))
            entries, bodies = self._cache._entries(self.path, tree)
            return {name: bodies[sha] for name, (_, sha) in entries.items()}

    async def _push(self, branch: str, commit: str, expected_head: str | None) -> dict[str, Any]:
        if expected_head is not None:
            try:
                self._cache._git(["merge-base", "--is-ancestor", expected_head, commit],
                                 git_dir=self.path)
            except RepositoryError:
                raise RemoteHeadMoved("Sync publication must be a fast-forward of the expected head") from None
        current = await self._remote_head(branch)
        if current == commit:
            return {"head": commit, "changed": False}
        if current != expected_head:
            raise RemoteHeadMoved("Remote branch changed; reconcile it before retrying publication")
        ref = f"refs/heads/{branch}"
        try:
            await self._remote_git([
                "push", "--porcelain", "--no-verify", "--atomic", "--no-follow-tags",
                f"--force-with-lease={ref}:{expected_head or ''}", "--", self.remote, f"{commit}:{ref}",
            ])
        except GitSyncError:
            after = await self._remote_head(branch)
            if after == commit:
                return {"head": commit, "changed": False}
            if after != expected_head:
                raise RemoteHeadMoved("Remote branch changed while publication was being pushed") from None
            raise
        if await self._remote_head(branch) != commit:
            raise RemoteHeadMoved("Remote branch changed immediately after publication")
        return {"head": commit, "changed": True}

    def _copy_reviewed_source(self, repository: RepositoryStore, commit: str) -> tuple[
            dict[str, tuple[str, str]], dict[str, bytes]]:
        """Retain original review commits and source modes in the independent cache."""
        with repository._locked():
            repository._owned()
            try:
                repository._git(["merge-base", "--is-ancestor", commit, repository._head()],
                                git_dir=repository.path)
            except RepositoryError:
                raise GitSyncError("Only commits already published by the review repository may sync") from None
            repository._entries(repository.path, repository._commit_tree(repository.path, commit))
            pack = repository._git(["pack-objects", "--stdout", "--revs", "--no-reuse-delta"],
                                   git_dir=repository.path, body=f"{commit}\n".encode("ascii"))
        if len(pack) > _MAX_OUTPUT:
            raise GitSyncError("Reviewed Git object transfer exceeds the output limit")
        self._cache._git(["index-pack", "--stdin", "--fix-thin"], git_dir=self.path, body=pack)
        return self._cache._entries(self.path, self._cache._commit_tree(self.path, commit))

    async def publish_project(self, repository: RepositoryStore, source_commit: str, branch: str,
                              state_files: dict[str, bytes], expected_head: str | None,
                              message: str, on_prepared: Callable[[str], Awaitable[None]]) -> dict[str, Any]:
        """Publish reviewed source plus taskhub/ as one atomic, recoverable update.

        Only this remote presentation commit contains the generated state. Its
        parents retain the remote history and the exact accepted source commit;
        the local review repository and its evidence bindings never change.
        """
        branch, source_commit = validate_branch(branch), self._cache._oid(source_commit)
        if expected_head is not None:
            expected_head = self._cache._oid(expected_head)
        if (not isinstance(state_files, dict)
                or any(not isinstance(body, bytes) for body in state_files.values())
                or sum(len(body) for body in state_files.values()) > _MAX_STATE_BYTES):
            raise GitSyncError("Generated sync state exceeds its separate export byte limit")
        names = _paths(list(state_files), max_files=_MAX_STATE_FILES)
        if (not isinstance(message, str) or not message or "\0" in message
                or len(message.encode("utf-8")) > 65536):
            raise GitSyncError("A bounded nonempty sync commit message is required")
        async with self._locked():
            with self._cache._mutex:
                self._owned()
                source_entries, bodies = self._copy_reviewed_source(repository, source_commit)
                if len(source_entries) > MAX_SNAPSHOT_FILES:
                    raise GitSyncError("Reviewed source exceeds the supported source file limit")
                if any(name.split("/", 1)[0].upper().casefold() == STATE_DIRECTORY for name in source_entries):
                    raise GitSyncError("Reviewed source uses reserved taskhub/; relocate that source before syncing")
                total = sum(len(bodies[sha]) for _, sha in source_entries.values())
                if total > MAX_SNAPSHOT_BYTES:
                    raise GitSyncError("Reviewed source exceeds the supported source byte limit")
                if total + sum(len(body) for body in state_files.values()) > _MAX_SYNC_BYTES:
                    raise GitSyncError("Combined source and TaskHub export exceed their combined byte limit")
                entries = {**source_entries, **{
                    f"{STATE_DIRECTORY}/{name}": ("100644", self._cache._hash_bytes(state_files[name], self.path))
                    for name in names}}
                tree = self._cache._tree(entries, self.path)
                current = await self._fetch(branch)
                parents = [expected_head] if expected_head is not None else []
                # Do not add redundant merge parents to state-only updates.
                if expected_head != source_commit and (expected_head is None or self._cache._git(
                        ["merge-base", source_commit, expected_head], git_dir=self.path,
                        allowed=(0, 1)).decode("ascii").strip() != source_commit):
                    parents.append(source_commit)
                args = ["commit-tree", tree]
                for parent in parents:
                    args.extend(["-p", parent])
                commit = self._cache._oid(self._cache._git(
                    args, git_dir=self.path, body=message.encode("utf-8")).decode("ascii").strip())
                request = {"format": 1, "kind": "project-with-taskhub-directory", "branch": branch,
                           "expected_head": expected_head, "source_commit": source_commit,
                           "tree": tree, "message_sha256": hashlib.sha256(message.encode()).hexdigest()}
                key = hashlib.sha256(canonical(request).encode()).hexdigest()
                journal = self.control / "publications" / f"{key}.json"
                record = {"request": request, "commit": commit}
                if journal.exists() and self._cache._read_json(journal) != record:
                    raise GitSyncError("The sync publication journal was modified")
                if current not in {expected_head, commit}:
                    raise RemoteHeadMoved("Remote branch changed; reconcile it before publishing the project")
                self._cache._write_json(journal, record)
                # Persist the exact candidate before any push. Recovery requires
                # this commit ID, never merely matching file bodies or a marker.
                await on_prepared(commit)
                return await self._push(branch, commit, expected_head)
