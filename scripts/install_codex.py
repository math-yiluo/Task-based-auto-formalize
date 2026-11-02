"""Install the audited Codex release into this checkout's .tools, laid out as npm installs it.

Run it with the checkout's Python on Linux (x86_64 or arm64). It downloads the release's
launcher package and the machine's native package from the npm registry, refuses any whose
sha512 differs from the one pinned here (from the release's npm lockfile), and unpacks them
under .tools/codex-<version>/node_modules/@openai. It installs nothing else and runs no Codex
command; `taskctl init` then selects this installation before any Codex on PATH.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import platform
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.160.0"
REGISTRY = "https://registry.npmjs.org/@openai/codex/-/"
PACKAGES = {
    "codex": ("codex-0.160.0.tgz",
              "sha512-kEtVGzjRAYAMOwJxN39bGcna7LT3IDQgq64NNJ/dDTfu4OzZaocJcyNb5/gGJ/IVF/Vj7oK7E2m3nmTan7lpjg=="),
    "codex-linux-x64": ("codex-0.160.0-linux-x64.tgz",
                        "sha512-KI/73OqGrHmR18s7ya7E1NqV6rT0y3lxr0s8S1qR2m6zU6QRF/HlR529jALLh5vjdUnsRT4Ahoxt0axb4kY99g=="),
    "codex-linux-arm64": ("codex-0.160.0-linux-arm64.tgz",
                          "sha512-VnVdsS06YlDsL8OwaJjQ3xdqdJNG4i+eBJBV3LytddknzSaF/urijLzVg3ptkwvoj8C7Gn7iWpVE+y8WEaPiCg=="),
}
# Machine names, and the package and target CPU of their native build.
ARCHITECTURES = {"x86_64": ("x64", "x86_64"), "amd64": ("x64", "x86_64"),
                 "aarch64": ("arm64", "aarch64"), "arm64": ("arm64", "aarch64")}


def fetch(name: str) -> bytes:
    """A package's archive, exactly as pinned."""
    filename, integrity = PACKAGES[name]
    with urllib.request.urlopen(REGISTRY + filename, timeout=600) as response:
        body = response.read()
    if "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode("ascii") != integrity:
        raise SystemExit(f"{filename} does not match its pinned sha512; nothing was installed")
    return body


def unpack(body: bytes, destination: Path) -> None:
    """The archive's `package/` directory, as `destination`; nothing outside it."""
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            if member.name.startswith("package/") and member.name != "package/":
                member.name = member.name.removeprefix("package/")
                members.append(member)
        archive.extractall(destination, members=members, filter="data")


def install(root: Path) -> Path:
    """Install into the checkout at `root`; the native binary's path."""
    machine = platform.machine().lower()
    if sys.platform != "linux" or machine not in ARCHITECTURES:
        raise SystemExit(f"This installs Codex for Linux on x86_64 or arm64, not {sys.platform} {machine}")
    package, cpu = ARCHITECTURES[machine]
    names = ("codex", f"codex-linux-{package}")
    openai = root / ".tools" / f"codex-{VERSION}" / "node_modules" / "@openai"
    staging = openai.parent / ".installing"
    shutil.rmtree(staging, ignore_errors=True)
    archives = {name: fetch(name) for name in names}  # Every archive verified before any is unpacked.
    for name, body in archives.items():
        unpack(body, staging / name)
    native = staging / names[1] / "vendor" / f"{cpu}-unknown-linux-musl" / "bin" / "codex"
    if not native.is_file():
        shutil.rmtree(staging)
        raise SystemExit("The native package has no codex binary where npm installs it")
    openai.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.rmtree(openai / name, ignore_errors=True)
        (staging / name).rename(openai / name)
    staging.rmdir()
    return openai / names[1] / "vendor" / f"{cpu}-unknown-linux-musl" / "bin" / "codex"


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(install(Path(__file__).resolve().parents[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
