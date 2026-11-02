"""Keep the shared advisory graph tree outside formal source snapshots."""
from __future__ import annotations

from pathlib import Path

GRAPH_DIRECTORY = "graphs"


def is_graph_path(path: str) -> bool:
    """Reserve the top-level source entry, including its case aliases."""
    return path.split("/", 1)[0].upper().casefold() == GRAPH_DIRECTORY


def _resolved(path: Path) -> Path:
    return path.resolve()


def is_shared_graph_directory(root: Path, project_root: Path) -> bool:
    """Detect this project's graph root even when capture would strip its prefix."""
    root, project_root = _resolved(root), _resolved(project_root)
    if root.is_relative_to(project_root):
        relative = root.relative_to(project_root)
        if relative.parts and is_graph_path(relative.as_posix()):
            return True
    return root.is_relative_to(_resolved(project_root / GRAPH_DIRECTORY))
