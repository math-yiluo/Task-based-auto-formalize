"""Prepare one instance's pinned Lean/Mathlib cache before admitting workers.

Only this explicit bootstrap operation downloads or builds. Runtime readiness
checks use the small receipt, identities, and sentinels; content registration and
formal validation retain their separate full-manifest validation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import portalocker

from .bootstrap_pins import (
    CACHE_KEY,
    LEAN_COMMIT,
    LEAN_TOOLCHAIN,
    MATHLIB_COMMIT,
    MATHLIB_REPOSITORY,
    pinned_versions,
)
from .coordination.runtime.process import OwnedProcessHost
from .coordination.storage import atomic_write, collected, trusted_directory

_DIRECTORY = "lm-" + hashlib.sha256(CACHE_KEY.encode()).hexdigest()[:12]
_OWNER = ".taskbased-checkout.json"
_MAX_JSON = 1024 * 1024
_LOG_BYTES = 64 * 1024
_DISK_RESERVE = 2 * 1024 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


class CacheError(ValueError):
    """The pinned instance cache cannot establish its exact ready state."""


class _Incomplete(CacheError):
    """An owned cache is missing an installation or prepared build artifact."""


@dataclass(frozen=True)
class _Result:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


def _path(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    for item in (path, *path.parents):
        if trusted_directory(item):
            break  # The project root and its ancestors were verified once.
        if item.is_symlink():
            raise CacheError(f"Linked cache paths are unsupported: {item}")
    return path


def _read_json(path: Path) -> dict:
    path = _path(path)
    if not path.is_file():
        raise _Incomplete(f"Missing cache evidence: {path}")
    if path.stat().st_size > _MAX_JSON:
        raise CacheError(f"Oversized cache evidence: {path}")
    try:
        data = json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as error:
        raise CacheError(f"Invalid cache evidence: {path}") from error
    if not isinstance(data, dict):
        raise CacheError(f"Cache evidence must be a JSON object: {path}")
    return data


def _write_json(path: Path, value: dict) -> None:
    path = _path(path)
    body = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(body) > _MAX_JSON:
        raise CacheError("Cache evidence exceeds its retained size limit")
    atomic_write(path, body, prefix=".bootstrap-")


def _owner() -> dict:
    return {"schema_version": 1, "cache_key": CACHE_KEY, "pins": pinned_versions()}


@asynccontextmanager
async def _cache_lock(path: Path):
    # Nonblocking attempts keep cancellation and progress handling responsive;
    # the lock has no build-duration deadline and is released on process exit.
    with _path(path).open("a+b") as stream:
        while True:
            try:
                portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
                break
            except portalocker.exceptions.LockException:
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            portalocker.unlock(stream)


async def _run(argv: list[str], *, cwd: Path, env: dict[str, str], stdin: bytes | None = None) -> _Result:
    """Run a native argv with owned process cleanup and bounded retained tails.

    Builds have no deadline. Verbose output is drained continuously rather than
    terminating an otherwise successful build when its log becomes large.
    """
    _check_disk_space(cwd)
    host = await OwnedProcessHost.start(argv, cwd=cwd, env=env, max_frame_bytes=1048576)
    tails = [bytearray(), bytearray()]
    truncated = False

    async def read(stream, index):
        nonlocal truncated
        while chunk := await stream.read(8192):
            tails[index].extend(chunk)
            if len(tails[index]) > _LOG_BYTES:
                del tails[index][:-_LOG_BYTES]
                truncated = True

    async def feed():
        try:
            if stdin is not None:
                host.stdin.write(stdin)
                await host.stdin.drain()
            host.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    jobs = [asyncio.create_task(read(host.stdout, 0)), asyncio.create_task(read(host.stderr, 1)),
            asyncio.create_task(feed()), asyncio.create_task(host.process.wait())]
    async def disk_space():
        while True:
            await asyncio.sleep(2)
            _check_disk_space(cwd)
    disk = asyncio.create_task(disk_space())
    try:
        # A leaked descendant must not keep pipe readers alive indefinitely once
        # the actual command has ended; stop the owned tree before collecting.
        done, _ = await asyncio.wait({jobs[-1], disk}, return_when=asyncio.FIRST_COMPLETED)
        if disk in done:
            disk.result()
        stopped = await host.stop(grace=0.1)
        await asyncio.gather(*jobs)
        if not stopped.tree_quiet or stopped.forced:
            raise CacheError("Cache command left running descendants; readiness was not recorded")
    finally:
        async def clean():
            try:
                await host.stop(grace=0.1)
            finally:
                for job in [*jobs, disk]:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, disk, return_exceptions=True)
        await collected(asyncio.create_task(clean()))
    return _Result(host.process.returncode, tails[0].decode("utf-8", errors="replace"),
                   tails[1].decode("utf-8", errors="replace"), truncated)


def _check_disk_space(directory: Path) -> None:
    if shutil.disk_usage(directory).free < _DISK_RESERVE:
        raise CacheError("Dependency preparation stopped with less than 2 GiB free disk space; "
                         "free space and retry. Existing downloads are retained and no readiness is granted.")


def _environment(directory: Path) -> dict[str, str]:
    excluded = ("LEAN_", "LAKE_", "MATHLIB_", "GIT_")
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(excluded) and key.upper() != "ELAN_TOOLCHAIN"}
    environment.update({"ELAN_TOOLCHAIN": LEAN_TOOLCHAIN, "NO_COLOR": "1",
        "MATHLIB_CACHE_DIR": str(directory / "ltar"), "LAKE_CACHE_DIR": str(directory / "lake-cache"),
        "XDG_CACHE_HOME": str(directory / "downloads"), "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_COUNT": "0", "GCM_INTERACTIVE": "Never"})
    return environment


class _Preparation:
    def __init__(self, directory: Path, progress: Callable[[str], Any] | None):
        self.directory = directory
        self.progress = progress
        self.environment = _environment(directory)
        self.git = self.executable("git")

    @staticmethod
    def executable(name: str) -> str:
        value = shutil.which(name)
        if value is None:
            raise CacheError(f"Install {name} before preparing the Lean cache")
        return str(Path(value).resolve())

    def notify(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    async def command(self, argv: list[str], *, cwd: Path | None = None,
                      env: dict[str, str] | None = None, stdin: bytes | None = None,
                      check: bool = True) -> _Result:
        result = await _run(argv, cwd=cwd or self.directory, env=env or self.environment, stdin=stdin)
        _write_json(self.directory / "last-command.json", {
            "argv": argv, "returncode": result.returncode, "stdout": result.stdout[-_LOG_BYTES:],
            "stderr": result.stderr[-_LOG_BYTES:], "truncated": result.truncated})
        if check and result.returncode != 0:
            raise CacheError(f"Cache command failed ({Path(argv[0]).name}); "
                             f"see {self.directory / 'last-command.json'}: {result.stderr[-2000:]}")
        return result

    async def git_command(self, root: Path, *args: str, check: bool = True) -> _Result:
        return await self.command([self.git, "-c", "core.autocrlf=false",
            "-c", f"core.hooksPath={self.directory / 'empty-hooks'}", "-c", "maintenance.auto=false",
            "-c", "gc.auto=0", "-c", "protocol.file.allow=never", "-C", str(root), *args], check=check)

    async def checkout(self, root: Path, url: str, commit: str) -> None:
        root = _path(root)
        marker = {"schema_version": 1, "url": url, "commit": commit}
        if root.exists() and not (root / _OWNER).exists() and any(root.iterdir()):
            raise CacheError(f"Refusing an unowned dependency directory: {root}")
        root.mkdir(parents=True, exist_ok=True)
        if (root / _OWNER).exists():
            if _read_json(root / _OWNER) != marker:
                raise CacheError(f"Dependency checkout ownership or pins changed: {root}")
        else:
            _write_json(root / _OWNER, marker)
        if not (root / ".git").exists():
            await self.git_command(root, "init", "--quiet")
        if not _path(root / ".git").is_dir():
            raise CacheError(f"Expected an owned ordinary Git checkout: {root}")
        remote = await self.git_command(root, "remote", "get-url", "origin", check=False)
        if remote.returncode != 0:
            await self.git_command(root, "remote", "add", "origin", url)
        elif remote.stdout.strip() != url:
            raise CacheError(f"Dependency repository URL changed: {root}")
        head = await self.git_command(root, "rev-parse", "--verify", "HEAD", check=False)
        if head.returncode == 0:
            if head.stdout.strip() != commit:
                raise CacheError(f"Dependency HEAD differs from its pinned commit: {root}")
        else:
            present = await self.git_command(root, "cat-file", "-e", commit + "^{commit}", check=False)
            if present.returncode != 0:
                self.notify("Fetching pinned source: " + root.name)
                await self.git_command(root, "fetch", "--no-tags", "--depth=1", "origin", commit)
            await self.git_command(root, "checkout", "--quiet", "--detach", commit)
        await self.check_checkout(root, url, commit)

    async def check_checkout(self, root: Path, url: str, commit: str) -> None:
        if (await self.git_command(root, "rev-parse", "HEAD")).stdout.strip() != commit:
            raise CacheError(f"Dependency commit changed: {root}")
        if (await self.git_command(root, "remote", "get-url", "origin")).stdout.strip() != url:
            raise CacheError(f"Dependency repository changed: {root}")
        if _path(root / ".git/HEAD").read_text(encoding="ascii").strip() != commit:
            raise CacheError(f"Dependency checkout is not detached at its exact pin: {root}")
        status = (await self.git_command(root, "status", "--porcelain", "--untracked-files=all")).stdout
        if any(line and line != "?? " + _OWNER for line in status.splitlines()):
            raise CacheError(f"Pinned dependency contains modified tracked source: {root}")

    async def toolchain(self, previous: dict | None = None) -> tuple[Path, Path]:
        self.notify("Resolving the exact Lean toolchain")
        if previous and Path(previous["lean_executable"]).is_file() and Path(previous["lake_executable"]).is_file():
            lean, lake = Path(previous["lean_executable"]), Path(previous["lake_executable"])
        else:
            elan = self.executable("elan")
            local = {**self.environment, "ELAN_HOME": str(self.directory / "elan")}
            environments = [local, self.environment] if (self.directory / "elan").exists() else [self.environment]
            result = _Result(1)
            for environment in environments:
                # `elan which` can INSTALL an absent override implicitly. The
                # explicit run form without --install is a read-only probe.
                result = await self.command([elan, "run", LEAN_TOOLCHAIN, "lean", "--print-prefix"],
                                            env=environment, check=False)
                if result.returncode == 0:
                    break
            # New toolchain downloads stay with this instance; never change the
            # user's default, override, or global Elan installation.
            if result.returncode != 0:
                await self.command([elan, "toolchain", "install", LEAN_TOOLCHAIN], env=local)
                result = await self.command([elan, "run", LEAN_TOOLCHAIN, "lean", "--print-prefix"], env=local)
            prefix = Path(result.stdout.strip())
            if not prefix.is_absolute():
                raise CacheError("Elan did not return an absolute toolchain installation")
            lean = (prefix / "bin" / "lean").resolve()
            lake = lean.with_name("lake")
        if not lean.is_absolute() or not lean.is_file() or not lake.is_file():
            raise CacheError("Elan did not resolve the real Lean/Lake toolchain executables")
        await self.check_lean(lean)
        self.environment["PATH"] = str(lean.parent) + os.pathsep + self.environment.get("PATH", "")
        self.environment["LEAN_SYSROOT"] = str(lean.parent.parent)
        return lean, lake

    async def check_lean(self, lean: Path) -> None:
        actual = (await self.command([str(lean), "--githash"])).stdout.strip()
        if actual != LEAN_COMMIT:
            raise CacheError(f"Lean commit must be {LEAN_COMMIT}, observed {actual!r}")

    async def import_mathlib(self, lean: Path, roots: list[str]) -> bool:
        self.notify("Checking a full Mathlib import with the pinned Lean executable")
        environment = {**self.environment, "LEAN_PATH": os.pathsep.join(roots)}
        # No -o and no source file: this check cannot add build artifacts to the
        # registered roots, including when rechecking a previously ready cache.
        result = await self.command([str(lean), "--stdin"], env=environment,
                                    stdin=b"import Mathlib\n", check=False)
        return result.returncode == 0


def _manifest(root: Path) -> tuple[str, list[dict[str, str]]]:
    path = _path(root / "lake-manifest.json")
    document = _read_json(path)
    if document.get("packagesDir") != ".lake/packages" or document.get("lakeDir", ".lake") != ".lake":
        raise CacheError("The pinned Mathlib manifest uses an unexpected cache layout")
    packages = document.get("packages")
    if not isinstance(packages, list) or len(packages) > 100:
        raise CacheError("The pinned Mathlib dependency manifest is malformed")
    result = []
    seen = set()
    for item in packages:
        if not isinstance(item, dict):
            raise CacheError("The pinned manifest contains a malformed dependency")
        name, url, revision = item.get("name"), item.get("url"), item.get("rev")
        if (not isinstance(name, str) or _NAME.fullmatch(name) is None or name.casefold() in seen
                or not isinstance(url, str) or re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url) is None
                or not isinstance(revision, str) or _SHA.fullmatch(revision) is None
                or item.get("type") != "git" or item.get("subDir") not in (None, "")):
            raise CacheError("Dependencies must have unique portable names and exact HTTPS Git commits")
        seen.add(name.casefold())
        source = _path(root / ".lake/packages" / name)
        result.append({"name": name, "url": url, "commit": revision, "source_root": str(source),
                       "library_root": str(source / ".lake/build/lib/lean")})
    return hashlib.sha256(path.read_bytes()).hexdigest(), result


def _sentinels(roots: list[str]) -> list[dict[str, Any]]:
    result = []
    for index, value in enumerate(roots):
        root = _path(Path(value))
        if not root.is_dir():
            raise _Incomplete(f"Missing prebuilt library root: {root}")
        sentinel = root / "Mathlib.olean" if index == 0 else next(root.rglob("*.olean"), None)
        if sentinel is None or not _path(sentinel).is_file() or sentinel.stat().st_size <= 0:
            raise _Incomplete(f"Missing compiled library artifacts: {root}")
        result.append({"path": str(sentinel), "size": sentinel.stat().st_size})
    return result


def _available_library_roots(mathlib: Path, dependencies: list[dict[str, str]]) -> list[str]:
    """Use built import roots; a locked build-only package need not have oleans.

    Keep Mathlib first even before its artifacts exist so the real import check
    diagnoses missing Mathlib. The final sentinel check requires its own olean.
    """
    roots = [str(mathlib / ".lake/build/lib/lean")]
    for dependency in dependencies:
        root = _path(Path(dependency["library_root"]))
        if root.is_dir() and any(path.is_file() and path.stat().st_size > 0
                                 for path in root.rglob("*.olean")):
            roots.append(str(root))
    return roots


def validate_cache(receipt: dict) -> dict:
    """Validate small immutable identities and presence, without builds or hashing libraries.

    This is a readiness gate, not a replacement for the environment's registered
    full content manifests. Call prepare_cache explicitly for a real import check.
    """
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("pins") != pinned_versions():
        raise CacheError("Cache receipt pins or schema differ from the supported baseline")
    try:
        directory = _path(Path(receipt["cache_directory"]))
        if not Path(receipt["cache_directory"]).is_absolute():
            raise CacheError("Cache directory must be absolute")
        if receipt["receipt_path"] != str(directory / "ready.json"):
            raise CacheError("Cache receipt path differs from its owned directory")
        if _read_json(directory / "owner.json") != _owner():
            raise CacheError("Cache owner identity changed")
        if _read_json(directory / "ready.json") != receipt:
            raise CacheError("The saved ready receipt differs from the requested cache")
        mathlib = _path(directory / "mathlib")
        if receipt["mathlib_directory"] != str(mathlib):
            raise CacheError("Mathlib directory differs from the owned cache")
        manifest_sha, dependencies = _manifest(mathlib)
        if receipt["manifest_sha256"] != manifest_sha or receipt["dependencies"] != dependencies:
            raise CacheError("The locked Mathlib dependency manifest changed")
        sources = [str(mathlib), *(item["source_root"] for item in dependencies)]
        candidates = [str(mathlib / ".lake/build/lib/lean"), *(item["library_root"] for item in dependencies)]
        roots = receipt["library_roots"]
        if (receipt["source_roots"] != sources or not isinstance(roots, list) or not roots
                or roots[0] != candidates[0] or roots != [root for root in candidates if root in roots]):
            raise CacheError("The recorded source/library roots differ from the pinned lock")
        for root, url, commit in [(mathlib, MATHLIB_REPOSITORY, MATHLIB_COMMIT), *(
                (Path(item["source_root"]), item["url"], item["commit"]) for item in dependencies)]:
            if _read_json(root / _OWNER) != {"schema_version": 1, "url": url, "commit": commit}:
                raise CacheError("Pinned checkout ownership changed")
            head = _path(root / ".git/HEAD")
            if not head.is_file():
                raise _Incomplete(f"Missing dependency Git identity: {root}")
            if head.read_text(encoding="ascii").strip() != commit:
                raise CacheError(f"Dependency HEAD differs from its exact detached pin: {root}")
        if _path(mathlib / "lean-toolchain").read_text(encoding="utf-8").strip() != LEAN_TOOLCHAIN:
            raise CacheError("Mathlib requires a different Lean toolchain")
        for field in ("lean_executable", "lake_executable"):
            executable = _path(Path(receipt[field]))
            if not Path(receipt[field]).is_absolute() or not executable.is_file():
                raise _Incomplete(f"Missing pinned executable: {field}")
        if Path(receipt["lean_executable"]).parent != Path(receipt["lake_executable"]).parent:
            raise CacheError("Lean and Lake must come from the same real toolchain")
        sentinels = receipt["sentinels"]
        if not isinstance(sentinels, list) or len(sentinels) != len(roots):
            raise CacheError("Incomplete library sentinel identities")
        for root_value, item in zip(roots, sentinels, strict=True):
            sentinel = _path(Path(item["path"]))
            if (not sentinel.is_relative_to(root_value) or sentinel.suffix != ".olean"
                    or type(item["size"]) is not int or item["size"] <= 0):
                raise CacheError("Invalid compiled library sentinel")
            if not sentinel.is_file() or sentinel.stat().st_size != item["size"]:
                raise _Incomplete(f"Missing or changed prepared library artifact: {sentinel}")
        if Path(sentinels[0]["path"]) != Path(roots[0]) / "Mathlib.olean":
            raise CacheError("The ready receipt must bind the full Mathlib import")
        if receipt["verification"] != {"lean_commit": LEAN_COMMIT, "mathlib_commit": MATHLIB_COMMIT,
                                       "import": "Mathlib", "succeeded": True}:
            raise CacheError("The cache lacks successful exact import verification")
    except (KeyError, TypeError, IndexError, OSError, UnicodeError) as error:
        raise CacheError(f"Invalid or inaccessible pinned cache: {error}") from error
    return receipt


async def prepare_cache(cache_root: Path, *, progress: Callable[[str], Any] | None = None) -> dict:
    """Prepare the supported baseline inside an explicitly selected project cache.

    Each project instance shares this cache among its workers. An already
    installed exact Elan toolchain may be reused read-only; any needed new
    installation, Git checkout, Mathlib archive and build stays in cache_root.
    """
    root = _path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    locks = _path(root / ".locks")
    locks.mkdir(exist_ok=True)
    if progress:
        progress("Waiting for the instance's pinned dependency cache lock")
    async with _cache_lock(locks / (CACHE_KEY + ".lock")):
        directory = _path(root / _DIRECTORY)
        if directory.exists() and not (directory / "owner.json").exists() and any(directory.iterdir()):
            raise CacheError(f"Refusing an unowned cache directory: {directory}")
        directory.mkdir(exist_ok=True)
        if (directory / "owner.json").exists():
            if _read_json(directory / "owner.json") != _owner():
                raise CacheError("An existing cache belongs to different pins")
        else:
            _write_json(directory / "owner.json", _owner())
        _path(directory / "empty-hooks").mkdir(exist_ok=True)
        preparation = _Preparation(directory, progress)
        ready_path = directory / "ready.json"
        previous = _read_json(ready_path) if ready_path.exists() else None
        ready = False
        if previous is not None:
            try:
                validate_cache(previous)
                ready = True
            except _Incomplete:
                preparation.notify("Repairing missing prepared dependency artifacts")
        lean, lake = await preparation.toolchain(previous)
        mathlib = directory / "mathlib"
        await preparation.checkout(mathlib, MATHLIB_REPOSITORY, MATHLIB_COMMIT)
        if (mathlib / "lean-toolchain").read_text(encoding="utf-8").strip() != LEAN_TOOLCHAIN:
            raise CacheError("The pinned Mathlib checkout has a different lean-toolchain")
        manifest_sha, dependencies = _manifest(mathlib)
        for dependency in dependencies:
            await preparation.checkout(Path(dependency["source_root"]), dependency["url"], dependency["commit"])
        roots = _available_library_roots(mathlib, dependencies)
        if ready and previous is not None:
            if (str(lean) != previous["lean_executable"] or str(lake) != previous["lake_executable"]):
                raise CacheError("The resolved toolchain differs from the recorded ready cache")
            if await preparation.import_mathlib(lean, previous["library_roots"]):
                preparation.notify("Reusing the verified instance dependency cache")
                return validate_cache(previous)
            ready = False
            imported = False
        else:
            # A process may have stopped after cache extraction but before its
            # ready receipt. Verify these retained artifacts before downloading
            # or decompressing again; exact source pins were checked above.
            imported = await preparation.import_mathlib(lean, roots)
        # Invalidate the readiness marker before mutating an earlier cache.
        if previous is not None and not ready:
            _write_json(directory / "previous-ready.json", previous)
            ready_path.unlink()
        if not imported:
            preparation.notify("Downloading and unpacking the exact Mathlib build cache")
            await preparation.command([str(lake), "--no-cache", "exe", "cache", "get"], cwd=mathlib, check=False)
            roots = _available_library_roots(mathlib, dependencies)
            imported = await preparation.import_mathlib(lean, roots)
        if not imported:
            preparation.notify("Building the missing Mathlib import closure (no time limit)")
            # The module olean facet follows the actual import closure without
            # requesting every dependency package's unrelated default targets.
            await preparation.command([str(lake), "--no-cache", "build", "+Mathlib:olean"], cwd=mathlib)
            roots = _available_library_roots(mathlib, dependencies)
            if not await preparation.import_mathlib(lean, roots):
                raise CacheError(f"The prepared cache cannot import Mathlib; see {directory / 'last-command.json'}")
        sentinels = _sentinels(roots)
        preparation.notify("Mathlib import succeeded; verifying final pinned dependency identities")
        await preparation.check_lean(lean)
        await preparation.check_checkout(mathlib, MATHLIB_REPOSITORY, MATHLIB_COMMIT)
        for dependency in dependencies:
            await preparation.check_checkout(Path(dependency["source_root"]), dependency["url"], dependency["commit"])
        if _manifest(mathlib) != (manifest_sha, dependencies):
            raise CacheError("Lake changed the exact locked dependency manifest")
        receipt = {"schema_version": 1, "pins": pinned_versions(), "cache_directory": str(directory),
            "mathlib_directory": str(mathlib), "lean_executable": str(lean), "lake_executable": str(lake),
            "library_roots": roots, "source_roots": [str(mathlib), *(item["source_root"] for item in dependencies)],
            "manifest_sha256": manifest_sha, "dependencies": dependencies, "sentinels": sentinels,
            "verification": {"lean_commit": LEAN_COMMIT, "mathlib_commit": MATHLIB_COMMIT,
                             "import": "Mathlib", "succeeded": True}, "receipt_path": str(ready_path)}
        _write_json(ready_path, receipt)
        preparation.notify("The exact Lean/Mathlib dependency cache is ready")
        return validate_cache(receipt)
