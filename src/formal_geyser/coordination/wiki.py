"""Seed the small public knowledge entry point; never track worker reading."""
from pathlib import Path
from threading import RLock

from .content import _normal_path, _path, _write_new

LITERATURE_GUIDE = "wiki/literature.md"
LIBRARY_GUIDE = "wiki/library.md"
KNOWLEDGE_README = "wiki/README.md"
_TEMPLATES = {
    KNOWLEDGE_README: "knowledge-readme.md",
    LITERATURE_GUIDE: "literature-wiki.md",
    LIBRARY_GUIDE: "library-wiki.md",
}
_LOCK = RLock()


def ensure_guides(root: Path) -> dict[str, Path]:
    """Create missing starter pages, preserving every authored file."""
    with _LOCK:
        pages = {}
        for relative, template in _TEMPLATES.items():
            path = _path(root, relative)
            if not _normal_path(path).exists():
                _write_new(path, Path(__file__).with_name(template).read_bytes())
            pages[relative] = path
        return pages
