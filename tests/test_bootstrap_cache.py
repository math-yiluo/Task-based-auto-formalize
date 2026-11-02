"""Pinned cache workflows with fake native commands; no network or Lean workload."""
from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from formal_geyser import bootstrap_cache as cache
from formal_geyser.bootstrap_pins import LEAN_COMMIT, LEAN_TOOLCHAIN


class FakeCommands:
    """Synthetic external processes; production receipt/lock code remains active."""

    def __init__(self, root: Path):
        self.root = root
        self.prefix = root / "installed-toolchain"
        self._toolchain(self.prefix)
        self.calls: list[dict] = []
        self.remotes: dict[str, str] = {}
        self.cached = True
        self.build_ok = True
        self.import_ok = True
        self.installed = True
        self.local_prefix: Path | None = None
        self.lean_hash = LEAN_COMMIT
        self.gets = 0
        self.builds = 0
        self.fail_fetch_once = False
        self.mutate_manifest = False
        self.dirty = ""
        self.omit_outputs: set[str] = set()
        self.unused_packages: set[str] = set()
        self.manifest = {"version": "1.2.0", "packagesDir": ".lake/packages", "lakeDir": ".lake",
            "name": "mathlib", "fixedToolchain": True, "packages": [
                {"name": "batteries", "type": "git", "url": "https://github.com/leanprover-community/batteries",
                 "rev": "b" * 40, "inputRev": "main", "subDir": None},
                {"name": "Qq", "type": "git", "url": "https://github.com/leanprover-community/quote4",
                 "rev": "c" * 40, "inputRev": "master", "subDir": None}]}

    @staticmethod
    def _toolchain(prefix: Path):
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        for executable in ("lean", "lake"):
            (prefix / "bin" / executable).write_bytes(b"synthetic executable")

    @staticmethod
    def executable(name: str) -> str:
        return str(Path("/fixture") / name)

    def install(self, monkeypatch):
        monkeypatch.setattr(cache, "_run", self.run)
        monkeypatch.setattr(cache._Preparation, "executable", staticmethod(self.executable))

    def outputs(self, mathlib: Path):
        roots = [mathlib, *(mathlib / ".lake/packages" / item["name"] for item in self.manifest["packages"])]
        for index, root in enumerate(roots):
            if root.name in self.omit_outputs:
                continue
            output = root / ".lake/build/lib/lean"
            output.mkdir(parents=True, exist_ok=True)
            (output / ("Mathlib.olean" if index == 0 else root.name + ".olean")).write_bytes(b"synthetic verified olean")

    async def run(self, argv, *, cwd, env, stdin=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "stdin": stdin})
        name = Path(argv[0]).stem
        if name == "elan":
            if argv[1:3] == ["toolchain", "install"]:
                self.local_prefix = Path(env["ELAN_HOME"]) / "toolchains/pinned"
                self._toolchain(self.local_prefix)
                return cache._Result(0)
            assert argv[1:] == ["run", LEAN_TOOLCHAIN, "lean", "--print-prefix"]
            prefix = self.local_prefix if env.get("ELAN_HOME", "").endswith("elan") else self.prefix if self.installed else None
            return cache._Result(0, str(prefix)) if prefix else cache._Result(1, stderr="not installed")
        if name == "lean":
            if argv[1:] == ["--githash"]:
                return cache._Result(0, self.lean_hash)
            assert argv[1:] == ["--stdin"] and stdin == b"import Mathlib\n"
            roots = [Path(path) for path in env["LEAN_PATH"].split(os.pathsep)]
            mathlib = roots[0].parents[3]
            required = [roots[0], *(mathlib / ".lake/packages" / item["name"] / ".lake/build/lib/lean"
                for item in self.manifest["packages"] if item["name"] not in self.unused_packages)]
            okay = self.import_ok and all(root in roots and root.is_dir() and any(root.rglob("*.olean"))
                                           for root in required)
            return cache._Result(0 if okay else 1, stderr="" if okay else "missing compiled module")
        if name == "lake":
            assert argv[1] == "--no-cache"
            if argv[2:] == ["exe", "cache", "get"]:
                self.gets += 1
                downloads = Path(env["MATHLIB_CACHE_DIR"])
                downloads.mkdir(parents=True, exist_ok=True)
                (downloads / "download.ltar").write_bytes(b"retained download")
                await asyncio.sleep(0.02)
                if self.cached:
                    self.outputs(cwd)
                if self.mutate_manifest:
                    value = json.loads((cwd / "lake-manifest.json").read_text())
                    value["packages"][0]["rev"] = "d" * 40
                    (cwd / "lake-manifest.json").write_text(json.dumps(value))
                return cache._Result(0 if self.cached else 1, stderr="" if self.cached else "cache download incomplete")
            assert argv[2:] == ["build", "+Mathlib:olean"]
            self.builds += 1
            if self.build_ok:
                self.outputs(cwd)
            return cache._Result(0 if self.build_ok else 1, stderr="" if self.build_ok else "build failed")
        assert name == "git"
        root = Path(argv[argv.index("-C") + 1])
        args = argv[argv.index("-C") + 2:]
        if args == ["init", "--quiet"]:
            (root / ".git").mkdir()
            (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
        elif args == ["remote", "get-url", "origin"]:
            return cache._Result(0, self.remotes[str(root)]) if str(root) in self.remotes else cache._Result(2)
        elif args[:3] == ["remote", "add", "origin"]:
            self.remotes[str(root)] = args[3]
        elif args[0] == "rev-parse":
            head = (root / ".git/HEAD").read_text().strip()
            return cache._Result(0, head) if len(head) == 40 else cache._Result(128)
        elif args[:2] == ["cat-file", "-e"]:
            return cache._Result(0 if (root / ".git/fetched").exists() else 1)
        elif args[0] == "fetch":
            assert args[1:4] == ["--no-tags", "--depth=1", "origin"]
            if self.fail_fetch_once:
                self.fail_fetch_once = False
                return cache._Result(1, stderr="interrupted download")
            (root / ".git/fetched").write_text(args[-1])
        elif args[:3] == ["checkout", "--quiet", "--detach"]:
            (root / ".git/HEAD").write_text(args[3] + "\n")
            if root.name == "mathlib":
                (root / "lean-toolchain").write_text(LEAN_TOOLCHAIN + "\n")
                (root / "lake-manifest.json").write_text(json.dumps(self.manifest))
                (root / "Mathlib.lean").write_text("import Init\n")
                (root / "Mathlib.md").write_text("Synthetic upstream-source fixture.\n")
        elif args == ["status", "--porcelain", "--untracked-files=all"]:
            return cache._Result(0, self.dirty or "?? " + cache._OWNER + "\n")
        else:
            raise AssertionError(f"Unexpected fake command: {argv}")
        return cache._Result(0)


async def fake_prepared_cache(tmp_path, monkeypatch):
    """Reusable, explicitly synthetic ready-cache fixture for runtime gate tests."""
    fake = FakeCommands(tmp_path)
    fake.install(monkeypatch)
    receipt = await cache.prepare_cache(tmp_path / "project/.dependencies")
    assert cache.validate_cache(receipt) == receipt
    return receipt, fake


async def test_prepares_exact_instance_cache_and_reuses_without_download_or_build(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHLIB_CACHE_GET_URL", "https://untrusted.invalid/cache")
    monkeypatch.setenv("LEAN_PATH", "wrong/library")
    monkeypatch.setenv("ELAN_TOOLCHAIN", "wrong/toolchain")
    receipt, fake = await fake_prepared_cache(tmp_path, monkeypatch)
    assert fake.gets == 1 and fake.builds == 0
    assert all(Path(path).is_relative_to(tmp_path / "project/.dependencies") for path in receipt["source_roots"])
    assert receipt["library_roots"] == [str(Path(path) / ".lake/build/lib/lean") for path in receipt["source_roots"]]
    assert [item["commit"] for item in receipt["dependencies"]] == ["b" * 40, "c" * 40]
    assert all("MATHLIB_CACHE_GET_URL" not in call["env"] for call in fake.calls)
    assert all(call["env"]["ELAN_TOOLCHAIN"] == LEAN_TOOLCHAIN for call in fake.calls)
    assert not any("update" in call["argv"] or "default" in call["argv"] for call in fake.calls)
    fetches = sum("fetch" in call["argv"] for call in fake.calls)
    assert await cache.prepare_cache(tmp_path / "project/.dependencies") == receipt
    assert fake.gets == 1 and fake.builds == 0
    assert sum("fetch" in call["argv"] for call in fake.calls) == fetches
    imports = [call for call in fake.calls if call["stdin"] is not None]
    assert len(imports) == 3 and all(call["argv"][1:] == ["--stdin"] for call in imports)


async def test_missing_toolchain_installs_only_inside_instance(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.installed = False
    fake.install(monkeypatch)
    receipt = await cache.prepare_cache(tmp_path / "project/.dependencies")
    installs = [call for call in fake.calls if call["argv"][1:3] == ["toolchain", "install"]]
    assert len(installs) == 1
    assert Path(installs[0]["env"]["ELAN_HOME"]).is_relative_to(tmp_path / "project/.dependencies")
    assert Path(receipt["lean_executable"]).is_relative_to(tmp_path / "project/.dependencies")
    await cache.prepare_cache(tmp_path / "project/.dependencies")
    assert sum(call["argv"][1:3] == ["toolchain", "install"] for call in fake.calls) == 1


async def test_partial_download_builds_missing_roots_once(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.cached = False
    fake.install(monkeypatch)
    receipt = await cache.prepare_cache(tmp_path / ".dependencies")
    assert fake.gets == 1 and fake.builds == 1
    assert cache.validate_cache(receipt)["verification"]["succeeded"]


async def test_unused_locked_cli_without_outputs_does_not_trigger_build(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.manifest["packages"].append({"name": "Cli", "type": "git", "url": "https://github.com/leanprover/lean4-cli",
        "rev": "d" * 40, "inputRev": "v4.34.0", "subDir": None})
    fake.omit_outputs.add("Cli")
    fake.unused_packages.add("Cli")
    fake.install(monkeypatch)
    root = tmp_path / ".dependencies"
    receipt = await cache.prepare_cache(root)
    assert fake.gets == 1 and fake.builds == 0
    cli = next(item for item in receipt["dependencies"] if item["name"] == "Cli")
    assert cli["source_root"] in receipt["source_roots"]
    assert cli["library_root"] not in receipt["library_roots"]
    assert not Path(cli["library_root"]).exists()
    assert cache.validate_cache(receipt) == receipt
    assert await cache.prepare_cache(root) == receipt
    assert fake.gets == 1 and fake.builds == 0


async def test_missing_required_root_cannot_pass_the_actual_import_check(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.omit_outputs.add("Qq")
    fake.install(monkeypatch)
    root = tmp_path / ".dependencies"
    with pytest.raises(ValueError, match="cannot import Mathlib"):
        await cache.prepare_cache(root)
    assert fake.gets == fake.builds == 1
    assert not (root / cache._DIRECTORY / "ready.json").exists()


async def test_completed_extraction_without_receipt_is_verified_without_get_or_build(tmp_path, monkeypatch):
    receipt, fake = await fake_prepared_cache(tmp_path, monkeypatch)
    Path(receipt["receipt_path"]).unlink()  # Simulate interruption immediately before the ready write.
    recovered = await cache.prepare_cache(tmp_path / "project/.dependencies")
    assert cache.validate_cache(recovered) == recovered
    assert fake.gets == 1 and fake.builds == 0


async def test_failure_keeps_downloads_without_ready_and_retry_reuses_sources(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.cached = fake.build_ok = False
    fake.install(monkeypatch)
    root = tmp_path / ".dependencies"
    with pytest.raises(cache.CacheError, match="command failed"):
        await cache.prepare_cache(root)
    directory = root / cache._DIRECTORY
    assert not (directory / "ready.json").exists()
    assert (directory / "ltar/download.ltar").is_file()
    fetches = sum("fetch" in call["argv"] for call in fake.calls)
    fake.build_ok = True
    receipt = await cache.prepare_cache(root)
    assert sum("fetch" in call["argv"] for call in fake.calls) == fetches
    assert cache.validate_cache(receipt)


async def test_missing_ready_artifacts_are_repaired_and_old_marker_is_invalidated(tmp_path, monkeypatch):
    receipt, fake = await fake_prepared_cache(tmp_path, monkeypatch)
    Path(receipt["sentinels"][0]["path"]).unlink()
    with pytest.raises(ValueError, match="Missing or changed"):
        cache.validate_cache(receipt)
    repaired = await cache.prepare_cache(tmp_path / "project/.dependencies")
    assert fake.gets == 2 and cache.validate_cache(repaired)
    assert (Path(receipt["cache_directory"]) / "previous-ready.json").is_file()


async def test_failed_full_import_never_marks_cache_ready(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.import_ok = False
    fake.install(monkeypatch)
    root = tmp_path / ".dependencies"
    with pytest.raises(ValueError, match="cannot import Mathlib"):
        await cache.prepare_cache(root)
    assert fake.gets == fake.builds == 1
    assert not (root / cache._DIRECTORY / "ready.json").exists()


@pytest.mark.parametrize("failure", ["lean_commit", "mathlib_head", "dependency_head", "manifest", "dirty", "unowned"])
async def test_changed_pins_or_foreign_sources_fail_without_overwrite(tmp_path, monkeypatch, failure):
    receipt, fake = await fake_prepared_cache(tmp_path, monkeypatch)
    directory = Path(receipt["cache_directory"])
    if failure == "lean_commit":
        fake.lean_hash = "f" * 40
    elif failure in {"mathlib_head", "dependency_head"}:
        root = Path(receipt["source_roots"][0 if failure == "mathlib_head" else 1])
        (root / ".git/HEAD").write_text("d" * 40 + "\n")
    elif failure == "manifest":
        (directory / "mathlib/lake-manifest.json").write_text("{}")
    elif failure == "dirty":
        fake.dirty = " M Mathlib.lean\n"
    else:
        (directory / "owner.json").unlink()
    with pytest.raises(ValueError):
        await cache.prepare_cache(tmp_path / "project/.dependencies")
    assert fake.gets == 1 and fake.builds == 0


async def test_lake_must_not_rewrite_locked_dependency_commits(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.mutate_manifest = True
    fake.install(monkeypatch)
    with pytest.raises(ValueError, match="locked dependency manifest"):
        await cache.prepare_cache(tmp_path / ".dependencies")
    assert not (tmp_path / ".dependencies" / cache._DIRECTORY / "ready.json").exists()


async def test_concurrent_preparations_hold_one_cross_process_lock(tmp_path, monkeypatch):
    fake = FakeCommands(tmp_path)
    fake.install(monkeypatch)
    root = tmp_path / ".dependencies"
    first, second = await asyncio.gather(cache.prepare_cache(root), cache.prepare_cache(root))
    assert first == second and fake.gets == 1 and fake.builds == 0


async def test_runtime_validation_is_lightweight_and_checks_receipt_identity(tmp_path, monkeypatch):
    receipt, fake = await fake_prepared_cache(tmp_path, monkeypatch)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Runtime readiness must not execute a command")
    monkeypatch.setattr(cache, "_run", forbidden)
    assert cache.validate_cache(receipt) == receipt
    changed = deepcopy(receipt)
    changed["pins"]["mathlib_commit"] = "f" * 40
    with pytest.raises(ValueError, match="pins"):
        cache.validate_cache(changed)
    changed = deepcopy(receipt)
    changed["source_roots"].append(str(tmp_path))
    with pytest.raises(ValueError, match="saved ready receipt"):
        cache.validate_cache(changed)
    assert fake.gets == 1


@pytest.mark.parametrize("change", ["floating", "path", "duplicate", "subdir"])
async def test_manifest_cannot_trigger_floating_or_escaping_dependency_checkout(tmp_path, monkeypatch, change):
    fake = FakeCommands(tmp_path)
    if change == "floating":
        fake.manifest["packages"][0]["rev"] = "main"
    elif change == "path":
        fake.manifest["packages"][0]["name"] = "../outside"
    elif change == "duplicate":
        fake.manifest["packages"][1]["name"] = "BATTERIES"
    else:
        fake.manifest["packages"][0]["subDir"] = "../outside"
    fake.install(monkeypatch)
    with pytest.raises(ValueError, match="exact HTTPS Git commits"):
        await cache.prepare_cache(tmp_path / ".dependencies")
    assert fake.gets == 0


def test_cache_requires_an_explicit_instance_directory():
    with pytest.raises(TypeError):
        cache.prepare_cache()


def test_disk_reserve_preserves_room_for_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(cache.shutil, "disk_usage", lambda _: SimpleNamespace(free=cache._DISK_RESERVE - 1))
    with pytest.raises(ValueError, match="2 GiB"):
        cache._check_disk_space(tmp_path)
    monkeypatch.setattr(cache.shutil, "disk_usage", lambda _: SimpleNamespace(free=cache._DISK_RESERVE))
    cache._check_disk_space(tmp_path)
