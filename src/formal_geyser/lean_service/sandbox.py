"""Lean runs a source's own code while it compiles it, so the service's Lean and Lake processes
run in the Codex sandbox: they read what the user can read, but write only their build slot
(the working directory), the Lake artifact cache and the job's scratch directory. No model is
called and nothing is billed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping, Sequence


class Unavailable:
    """The configured sandbox cannot run here: every Lean process fails closed, as unavailable."""

    name = "unavailable"

    def __init__(self, reason: str):
        self.reason = reason

    def wrap(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
             writable: Sequence[Path]) -> tuple[list[str], dict[str, str]]:
        from ..registry import EnvironmentUnavailable

        raise EnvironmentUnavailable("The Lean service's sandbox is unavailable: " + self.reason)


class Unsandboxed:
    name = "none"

    def wrap(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
             writable: Sequence[Path]) -> tuple[list[str], dict[str, str]]:
        return list(argv), dict(env)


class CodexSandbox:
    name = "codex"

    def __init__(self, executable: str, home: Path,
                 protected: Sequence[str] | Callable[[], Sequence[str]] = ()):
        """`protected`: what Lean code may not read either (native_permissions.protected_reads), or
        how to find it, for each process: a sign-in made while the service runs is protected too."""
        from ..coordination.runtime.executable import resolve_codex_executable

        self.executable = resolve_codex_executable(executable)
        self.home = home
        self.protected = protected if callable(protected) else list(protected)
        home.mkdir(parents=True, exist_ok=True)

    def wrap(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str],
             writable: Sequence[Path]) -> tuple[list[str], dict[str, str]]:
        protected = self.protected() if callable(self.protected) else self.protected
        # /tmp (`:slash_tmp`) is memory here; Lean's temporary files go in a writable root.
        access = {":slash_tmp": "read", **dict.fromkeys(protected, "deny"),
                  **dict.fromkeys((str(Path(path).resolve()) for path in writable), "write")}
        table = "{" + ", ".join(f"{json.dumps(path)} = {json.dumps(mode)}" for path, mode in access.items()) + "}"
        command = [self.executable,
                   "-c", 'permissions.lean.extends=":workspace"', "-c", "permissions.lean.filesystem=" + table,
                   "sandbox", "-P", "lean", "-C", str(cwd), "--", *argv]
        return command, {**env, "CODEX_HOME": str(self.home)}
