"""Select an already installed, audited native runtime for a new Lean instance."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .coordination.models import AUDITED_CODEX_VERSION
from .coordination.runtime.executable import native_candidates, resolve_codex_executable


def _application_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _bundled_executables() -> list[Path]:
    """The application's own installation of the audited version (scripts/setup-linux.sh)."""
    return native_candidates(_application_root() / f".tools/codex-{AUDITED_CODEX_VERSION}/node_modules/@openai")


def select_bootstrap_codex(executable: str | None = None) -> tuple[str, str]:
    """Resolve and probe only; never install tools or change an existing config.

    An explicit selection takes precedence. Otherwise the application's own installation of
    the audited version precedes PATH, which may contain an unrelated newer CLI.
    """
    selected = executable
    if selected is None:
        selected = next((str(path) for path in _bundled_executables() if path.is_file()), "codex")
    native = Path(resolve_codex_executable(selected)).resolve()
    try:
        result = subprocess.run([str(native), "--version"], cwd=native.parent,
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("The selected native Codex executable could not report its version") from error
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", (result.stdout + result.stderr).strip())
    if result.returncode != 0 or match is None:
        raise ValueError("The selected native Codex executable did not report a successful version")
    version = match.group(1)
    if version != AUDITED_CODEX_VERSION:
        raise ValueError(f"Codex {version} is not the audited runtime version {AUDITED_CODEX_VERSION}; "
                         "select an existing native executable of that version with --codex-executable")
    return str(native), version
