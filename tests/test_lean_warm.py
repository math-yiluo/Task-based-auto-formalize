"""The Lean service keeps the library files that checks import resident."""
from __future__ import annotations

import os

from formal_geyser.lean_service.warm import WarmSet, source_root


def library(tmp_path, modules):
    """A package with sources and compiled files: {module: (imports, compiled bytes)}."""
    package = tmp_path / "pkg"
    lib = package / ".lake" / "build" / "lib" / "lean"
    for module, (imports, size) in modules.items():
        relative = module.replace(".", "/")
        source = package / (relative + ".lean")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("".join(f"import {name}\n" for name in imports) + "def x := 1\n", encoding="utf-8")
        olean = lib / (relative + ".olean")
        olean.parent.mkdir(parents=True, exist_ok=True)
        olean.write_bytes(b"\0" * size)
    return lib


def test_the_import_closure_of_the_most_used_modules_is_mapped_within_the_budget(tmp_path):
    lib = library(tmp_path, {"Lib.A": (["Lib.B"], 8192), "Lib.B": (["Lib.C"], 8192), "Lib.C": ([], 4096),
                             "Lib.Big": (["Lib.C"], 64 * 1024)})
    assert source_root(lib) == tmp_path / "pkg"
    warm = WarmSet(budget=32 * 1024)
    assert warm.note([lib], {"Lib.A"}) and not warm.note([lib], {"Lib.A"})
    warm.note([lib], {"Lib.Big"})
    assert warm.note([lib], {"Lib.A", "Project.Local"})  # A module of no library is ignored.
    planned = warm.plan()
    assert sorted(path.name for path in planned) == ["A.olean", "B.olean", "C.olean"]  # Big does not fit.
    warm.refresh()
    assert warm.status() == {"files": 3, "bytes": 20480, "budget": 32 * 1024, "imports": 3}
    warm.budget = 128 * 1024
    warm.refresh()
    assert warm.status()["files"] == 4
    warm.release()
    assert warm.status()["files"] == 0


def test_a_chosen_closure_is_not_walked_again(tmp_path, monkeypatch):
    lib = library(tmp_path, {"Lib.A": (["Lib.Base"], 4096), "Lib.B": (["Lib.Base"], 4096),
                             "Lib.Base": (["Lib.Root"], 4096), "Lib.Root": ([], 4096)})
    warm = WarmSet(budget=1 << 20)
    warm.note([lib], {"Lib.A", "Lib.B"})
    read: list[str] = []
    imports = WarmSet._source_imports
    monkeypatch.setattr(WarmSet, "_source_imports", lambda self, module: read.append(module) or imports(self, module))
    assert sorted(path.name for path in warm.plan()) == ["A.olean", "B.olean", "Base.olean", "Root.olean"]
    # Each header once; every Lean file imports Init implicitly.
    assert sorted(read) == ["Init", "Lib.A", "Lib.B", "Lib.Base", "Lib.Root"]


def test_other_libraries_start_a_new_record(tmp_path):
    lib = library(tmp_path, {"Lib.A": ([], 4096)})
    warm = WarmSet(budget=1 << 20)
    warm.note([lib], {"Lib.A"})
    warm.note([tmp_path / "other"], {"Other.X"})
    assert warm.plan() == [] and warm.status()["imports"] == 1


def test_mapped_files_hold_no_file_descriptor(tmp_path):
    modules = {f"Lib.M{index}": ([], 4096) for index in range(200)}
    lib = library(tmp_path, modules)
    warm = WarmSet(budget=1 << 30)
    warm.note([lib], set(modules))
    before = len(os.listdir("/proc/self/fd"))
    warm.refresh()
    try:
        assert warm.status()["files"] == 200 and len(os.listdir("/proc/self/fd")) < before + 10
    finally:
        warm.release()
