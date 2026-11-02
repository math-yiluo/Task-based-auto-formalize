"""Keep the library .olean files that checks import resident in memory.

A Lean process maps the compiled files of everything it imports. When their pages are not
in memory the import reads them from disk: in one measurement a check with typical Mathlib
imports took 182 s instead of 13.6 s, and `import Mathlib` 469 s instead of 30 s. The
service is long-running, so it maps the import closure of the library modules recent checks
used, most used first and within a budget, and touches every page now and then. The pages
are the same file pages every Lean process maps, so they stay resident for all of them. A
mapping holds no file descriptor: Mathlib alone has thousands of .olean files.
"""
from __future__ import annotations

import mmap
import threading
from collections import Counter
from pathlib import Path

from ..mathematical_artifacts import module_imports
from ..paths import module_path

LIBRARY_SUFFIX = (".lake", "build", "lib", "lean")
HEADER_BYTES = 64 * 1024  # Imports come first in a Lean file.


def source_root(library_root: Path) -> Path | None:
    """`<package>/.lake/build/lib/lean` compiles the sources under `<package>`."""
    parts = library_root.parts
    if tuple(part.casefold() for part in parts[-4:]) == LIBRARY_SUFFIX:
        return Path(*parts[:-4])
    return None


class WarmSet:
    def __init__(self, budget: int):
        self.budget = budget
        self.uses: Counter[str] = Counter()  # Library modules recent checks imported directly.
        self.roots: tuple[Path, ...] = ()
        self._imports: dict[Path, tuple[float, frozenset[str]]] = {}
        self._maps: dict[Path, tuple[mmap.mmap, int]] = {}
        self._lock = threading.Lock()
        self.modules = 0

    def note(self, library_roots: list[Path], imports: set[str]) -> bool:
        """A check of a project with these libraries imported these modules from them; whether
        any of them is new."""
        with self._lock:
            if tuple(library_roots) != self.roots:
                self.roots, self.uses = tuple(library_roots), Counter()
            new = bool(set(imports) - set(self.uses))
            self.uses.update(imports)
            return new

    def _source_imports(self, module: str) -> frozenset[str]:
        for root in self.roots:
            base = source_root(root)
            if base is None:
                continue
            path = base / module_path(module)
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            known = self._imports.get(path)
            if known is None or known[0] != stamp:
                try:
                    with path.open("rb") as stream:
                        header = stream.read(HEADER_BYTES).decode("utf-8-sig", errors="replace")
                    known = (stamp, frozenset(module_imports(header)))
                except (OSError, ValueError):
                    known = (stamp, frozenset())
                self._imports[path] = known
            return known[1]
        return frozenset()

    def _olean(self, module: str) -> Path | None:
        relative = module_path(module)[:-5] + ".olean"
        for root in self.roots:
            path = root / relative
            if path.is_file():
                return path
        return None

    def plan(self) -> list[Path]:
        """The .olean files to keep: closures of the most used imports first, within the budget."""
        with self._lock:
            ranked = [module for module, _ in self.uses.most_common()]
        chosen: dict[Path, int] = {}
        kept: set[str] = set()  # Modules whose whole closure is chosen already.
        total = 0
        for start in ranked:
            closure, pending = [], [start]
            seen: set[str] = set()
            while pending:
                module = pending.pop()
                # A chosen closure is not walked again: walking each start's closure in full
                # read the same Mathlib headers for every one of hundreds of imports, about
                # 0.6 of a core in the Linux pilot.
                if module in seen or module in kept:
                    continue
                seen.add(module)
                closure.append(module)
                pending.extend(self._source_imports(module))
            files = [path for path in map(self._olean, closure) if path is not None and path not in chosen]
            size = sum(path.stat().st_size for path in files)
            if total + size > self.budget:
                continue
            for path in files:
                chosen[path] = path.stat().st_size
            kept.update(closure)
            total += size
        self.modules = len(chosen)
        return list(chosen)

    def refresh(self) -> None:
        """Map the planned files, release the others, and touch every page."""
        wanted = self.plan()
        with self._lock:
            self._refresh(wanted)

    def _refresh(self, wanted: list[Path]) -> None:
        for path in list(self._maps):
            if path not in wanted:
                self._maps.pop(path)[0].close()
        for path in wanted:
            entry = self._maps.get(path)
            try:
                size = path.stat().st_size
                if entry is None or entry[1] != size:
                    if entry is not None:
                        entry[0].close()
                    with path.open("rb") as stream:
                        self._maps[path] = (mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ, trackfd=False),
                                            size)
                mapping = self._maps[path][0]
                bytes(memoryview(mapping)[::mmap.PAGESIZE])  # Reading one byte per page keeps it resident.
            except (OSError, ValueError):
                self._maps.pop(path, None)

    def release(self) -> None:
        with self._lock:
            for mapping, _ in self._maps.values():
                mapping.close()
            self._maps.clear()

    def status(self) -> dict:
        with self._lock:
            return {"files": len(self._maps), "bytes": sum(size for _, size in self._maps.values()),
                    "budget": self.budget, "imports": len(self.uses)}
