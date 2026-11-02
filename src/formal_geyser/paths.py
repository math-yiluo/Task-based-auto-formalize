from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def lean_name(value: str) -> str:
    """Use a deliberately portable subset of Lean names, never source interpolation."""
    if len(value) > 240 or not _NAME.fullmatch(value):
        raise ValueError("Expected an ASCII qualified Lean name")
    return value


def content_hash(value: str) -> str:
    if not _HASH.fullmatch(value):
        raise ValueError("Expected a lowercase SHA-256 digest")
    return value


def relative_path(value: str) -> str:
    if not value or len(value) > 240 or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Expected a portable relative POSIX path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or part.endswith((".", " ")) for part in parts):
        raise ValueError("Path has an empty, traversal, or ambiguous component")
    if any(part.split(".")[0].upper() in _RESERVED for part in parts):
        raise ValueError("Windows device names are not valid artifact paths")
    if any(any(ord(ch) < 32 or ch in '<>"|?*' for ch in part) for part in parts):
        raise ValueError("Path contains a nonportable character")
    if PurePosixPath(value).is_absolute():
        raise ValueError("Absolute artifact paths are not allowed")
    return value


def module_path(name: str) -> str:
    return lean_name(name).replace(".", "/") + ".lean"


def module_name(path: str) -> str:
    """The Lean module a source file defines: `A/B.lean` defines `A.B`."""
    return path.removesuffix(".lean").replace("/", ".")


def safe_child(root: Path, relative: str) -> Path:
    relative_path(relative)
    resolved_root = root.resolve()
    target = resolved_root.joinpath(*relative.split("/"))
    for parent in (target, *target.parents):
        if parent == resolved_root:
            break
        if parent.is_symlink():
            raise ValueError("Linked artifact paths are not allowed")
    if not target.resolve().is_relative_to(resolved_root):
        raise ValueError("Artifact path escapes its root")
    return target
