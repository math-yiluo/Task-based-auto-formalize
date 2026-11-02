"""The Codex installer takes only the pinned archives and lays them out as npm does; no test
reaches the network."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import os
import tarfile
from pathlib import Path

import pytest

from formal_geyser.coordination.runtime.executable import native_candidates

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_codex.py"


@pytest.fixture
def installer(monkeypatch):
    spec = importlib.util.spec_from_file_location("install_codex", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    return module


def archive(files: dict[str, tuple[bytes, int]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, (body, mode) in files.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(body), mode
            tar.addfile(member, io.BytesIO(body))
    return stream.getvalue()


def serve(installer, monkeypatch, bodies: dict[str, bytes], *, pinned: dict[str, bytes] | None = None):
    """The registry answers with `bodies`; the pins are those of `pinned` (default: `bodies`)."""
    def digest(body):
        return "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()

    monkeypatch.setattr(installer, "PACKAGES", {
        name: (name + ".tgz", digest((pinned or bodies)[name])) for name in bodies})

    def urlopen(url, timeout):
        return io.BytesIO(bodies[url.rsplit("/", 1)[1].removesuffix(".tgz")])
    monkeypatch.setattr(installer.urllib.request, "urlopen", urlopen)


LAUNCHER = archive({"package/bin/codex.js": (b"#!/usr/bin/env node\n", 0o755),
                    "package/package.json": (b"{}", 0o644)})
NATIVE = archive({"package/vendor/x86_64-unknown-linux-musl/bin/codex": (b"\x7fELF synthetic", 0o755)})


def test_the_pinned_release_is_installed_where_resolution_finds_it(installer, monkeypatch, tmp_path):
    serve(installer, monkeypatch, {"codex": LAUNCHER, "codex-linux-x64": NATIVE})
    native = installer.install(tmp_path)
    openai = tmp_path / ".tools" / "codex-0.160.0" / "node_modules" / "@openai"
    assert native.read_bytes() == b"\x7fELF synthetic" and os.access(native, os.X_OK)
    assert (openai / "codex" / "bin" / "codex.js").is_file()
    assert native in [path for path in native_candidates(openai) if path.is_file()]
    assert not (openai.parent / ".installing").exists()


def test_an_archive_that_differs_from_its_pin_installs_nothing(installer, monkeypatch, tmp_path):
    serve(installer, monkeypatch, {"codex": LAUNCHER, "codex-linux-x64": NATIVE},
          pinned={"codex": LAUNCHER, "codex-linux-x64": NATIVE + b"changed"})
    with pytest.raises(SystemExit, match="pinned sha512"):
        installer.install(tmp_path)
    assert not (tmp_path / ".tools" / "codex-0.160.0" / "node_modules" / "@openai" / "codex").exists()


def test_an_archive_cannot_write_outside_its_package(installer, monkeypatch, tmp_path):
    escaping = archive({"package/../../../../escaped": (b"outside", 0o644)})
    serve(installer, monkeypatch, {"codex": escaping, "codex-linux-x64": NATIVE})
    with pytest.raises(tarfile.TarError):
        installer.install(tmp_path)
    assert not list(tmp_path.rglob("escaped"))
