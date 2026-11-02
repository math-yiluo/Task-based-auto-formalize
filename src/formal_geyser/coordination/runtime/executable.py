"""Resolve the native Codex binary that an npm installation of @openai/codex contains."""
from __future__ import annotations

import platform
import shutil
from pathlib import Path

# Machine names and the npm package architecture and target CPU of their Linux build.
_ARCHITECTURES = {"x86_64": ("x64", "x86_64"), "amd64": ("x64", "x86_64"),
                  "aarch64": ("arm64", "aarch64"), "arm64": ("arm64", "aarch64")}


def native_candidates(openai: Path) -> list[Path]:
    """Where an npm installation keeps the native Linux binary, below its `@openai` directory:
    in the platform package beside or inside the launcher's package, or vendored in it."""
    architecture, cpu = _ARCHITECTURES.get(platform.machine().lower(), (None, None))
    if architecture is None:
        return []
    package, vendor = f"codex-linux-{architecture}", Path("vendor") / f"{cpu}-unknown-linux-musl" / "bin" / "codex"
    return [openai / package / vendor, openai / "codex" / "node_modules" / "@openai" / package / vendor,
            openai / "codex" / vendor]


def resolve_codex_executable(executable: str = "codex") -> str:
    """The native binary for `executable`, a path or a command name.

    npm installs a Node launcher (`bin/codex.js`) that starts the native binary; app-server
    talks over raw stdio, so the launcher is never run in its place.
    """
    candidate = Path(executable).expanduser()
    if candidate.is_file():
        resolved = candidate.resolve()
    else:
        found = shutil.which(executable)
        if found is None:
            raise FileNotFoundError(f"Codex executable was not found: {executable}")
        resolved = Path(found).resolve()
    if resolved.suffix != ".js":
        return str(resolved)
    for native in native_candidates(resolved.parent.parent.parent):
        if native.is_file():
            return str(native.resolve())
    raise FileNotFoundError(
        f"Found the npm launcher {resolved}, but no native Codex binary beside it. "
        "Set codex.executable to the installed native binary.")
