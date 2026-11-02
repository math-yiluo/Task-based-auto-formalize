from __future__ import annotations

import re

from .coordination.snapshot_limits import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES
from .paths import lean_name, module_name, module_path
from .registry import RegistryError, read_snapshot
from .schemas import LeanMarkdownBundle, MathematicalRecord

_RAW_STRING = re.compile(r'r(#+)?"')
_CHAR_LITERAL = re.compile(r"'(?:\\.|[^'\\\n])'")


def lean_tokens(source: str) -> list[str]:
    """Lex identifiers while excluding nested comments, literals, and raw strings.

    This rejects explicit proof holes and new axioms in authored source; it is not a transitive axiom audit.
    Unterminated comments/strings are rejected instead of hiding later source.
    """
    tokens: list[str] = []
    index = 0
    size = len(source)
    while index < size:
        if source.startswith("--", index):
            end = source.find("\n", index + 2)
            index = size if end < 0 else end + 1
            tokens.append("\n")
            continue
        if source.startswith("/-", index):
            depth = 1
            index += 2
            while index < size and depth:
                if source.startswith("/-", index):
                    depth += 1
                    index += 2
                elif source.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise RegistryError("Unterminated Lean comment")
            continue
        raw = _RAW_STRING.match(source, index)
        if raw:
            closing = '"' + (raw.group(1) or "")
            end = source.find(closing, index + len(raw.group()))
            if end < 0:
                raise RegistryError("Unterminated raw Lean string")
            index = end + len(closing)
            continue
        if source[index] == '"':
            index += 1
            while index < size and source[index] != '"':
                index += 2 if source[index] == "\\" else 1
            if index >= size:
                raise RegistryError("Unterminated Lean string")
            index += 1
            continue
        if source[index] == "'":
            char = _CHAR_LITERAL.match(source, index)
            if char:
                index += len(char.group())
                continue
        if source[index] == "«":
            end = source.find("»", index + 1)
            if end < 0:
                raise RegistryError("Unterminated quoted Lean identifier")
            tokens.append(source[index + 1:end])
            index = end + 1
            continue
        if source[index].isalpha() or source[index] == "_":
            end = index + 1
            while end < size and (source[end].isalnum() or source[end] in "_'?!"):
                end += 1
            # Commands such as `#eval` keep their `#`, unlike identifiers such as `Polynomial.eval`.
            tokens.append(("#" if index and source[index - 1] == "#" else "") + source[index:end])
            index = end
            continue
        if source[index] in ".\n":
            tokens.append(source[index])
        index += 1
    return tokens


PROOF_HOLES = frozenset({"sorry", "admit", "axiom", "sorryAx"})
# Constructs that record declarations the kernel never checked, or run code while the
# source compiles (which could alter other build outputs). Official validation replays
# every module through the kernel (leanchecker); rejecting these in submitted sources is
# a second, deliberately incomplete defense. The published Library uses none of them.
KERNEL_BYPASS = frozenset({
    "skipKernelTC", "addDeclWithoutChecking", "extern", "implemented_by",
    "#eval", "#eval!", "run_cmd", "run_elab", "run_meta", "initialize", "builtin_initialize",
})


def prohibited_constructs(source: str) -> tuple[set[str], set[str]]:
    """Proof holes and kernel-bypassing constructs, also inside dotted or quoted names."""
    parts = {part for token in lean_tokens(source) for part in {token, *token.split(".")}}
    return set(PROOF_HOLES & parts), set(KERNEL_BYPASS & parts)


# Pairing and hole checks depend only on the exact bytes each SHA-256 names, so a Lean/Markdown
# pair that passed once is not decoded and tokenized again in a later snapshot.
_CHECKED_PAIRS: set[tuple[str, str]] = set()
_CHECKED_PAIRS_LIMIT = 200_000


def validate_snapshot_sources(blobs, snapshot: str, *, bundle: LeanMarkdownBundle | None = None,
                              max_files: int = MAX_SNAPSHOT_FILES, max_bytes: int = MAX_SNAPSHOT_BYTES) -> list[dict[str, str]]:
    entries = read_snapshot(blobs, snapshot, max_files=max_files, max_bytes=max_bytes)
    files = {item["path"]: item["sha256"] for item in entries}
    for path, sha in files.items():
        if not path.lower().endswith(".lean"):
            continue
        if not path.endswith(".lean"):
            raise RegistryError("Lean source extensions must use canonical .lean spelling")
        if path.split("/")[0].split(".")[0] in {"Init", "Lean", "Std"}:
            raise RegistryError("Project source cannot shadow the registered toolchain modules")
        markdown = path[:-5] + ".md"
        if markdown not in files:
            raise RegistryError(f"Lean source requires same-stem Markdown: {path}")
        pair = (sha, files[markdown])
        if pair in _CHECKED_PAIRS:
            continue
        try:
            explanation = blobs.get(files[markdown]).decode("utf-8-sig")
            source = blobs.get(sha).decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise RegistryError("Lean and Markdown source must be UTF-8") from error
        if not explanation.strip():
            raise RegistryError(f"Paired Markdown must be nonempty: {markdown}")
        holes, bypass = prohibited_constructs(source)
        if holes:
            raise RegistryError(f"Lean source contains prohibited holes or axioms: {path}: {', '.join(sorted(holes))}")
        if bypass:
            raise RegistryError("Lean source uses constructs that bypass the kernel or run code while it "
                                f"compiles: {path}: {', '.join(sorted(bypass))}")
        if len(_CHECKED_PAIRS) >= _CHECKED_PAIRS_LIMIT:
            _CHECKED_PAIRS.clear()
        _CHECKED_PAIRS.add(pair)
    if bundle is not None:
        if bundle.snapshot != snapshot:
            raise RegistryError("Mathematical bundle snapshot differs from the authoritative snapshot")
        if files.get(bundle.lean_path) != bundle.lean_sha256:
            raise RegistryError("Mathematical bundle Lean path/hash differs from its snapshot")
        if files.get(bundle.markdown_path) != bundle.markdown_sha256:
            raise RegistryError("Mathematical bundle Markdown path/hash differs from its snapshot")
    return entries


def validate_math_record_snapshot(blobs, record: MathematicalRecord, snapshot: str | None = None,
                                  *, max_files: int = MAX_SNAPSHOT_FILES, max_bytes: int = MAX_SNAPSHOT_BYTES):
    return validate_snapshot_sources(blobs, snapshot or record.bundle.snapshot, bundle=record.bundle,
                                     max_files=max_files, max_bytes=max_bytes)


def module_imports(source: str) -> set[str]:
    """Read the portable subset of Lean 4.34's fixed module-header grammar.

    Module/Syntax.lean defines one identifier per import (independent of line
    breaks), with optional public/meta/all modifiers. Basic.lean permits escaped
    identifier components. We accept only the repository's ASCII module-name
    subset, including equivalent escaped components; unsupported spellings fail
    closed instead of being approximated. Imported source is never executed here.
    """
    word = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
    size = len(source)

    def space(index: int) -> int:
        while index < size:
            if source[index].isspace() or source[index] == "\ufeff":
                index += 1
            elif source.startswith("--", index):
                end = source.find("\n", index + 2)
                index = size if end < 0 else end + 1
            elif source.startswith("/-", index):
                depth = 1
                index += 2
                while index < size and depth:
                    if source.startswith("/-", index):
                        depth += 1
                        index += 2
                    elif source.startswith("-/", index):
                        depth -= 1
                        index += 2
                    else:
                        index += 1
                if depth:
                    raise RegistryError("Unterminated Lean header comment")
            else:
                break
        return index

    def keyword(index: int, value: str) -> int | None:
        index = space(index)
        match = word.match(source, index)
        if match is None or match.group() != value:
            return None
        end = match.end()
        if end < size and (source[end].isalnum() or source[end] in "_'?!."):
            return None
        return space(end)

    def identifier(index: int) -> tuple[str, int]:
        parts = []
        while True:
            if index < size and source[index] == "«":
                end = source.find("»", index + 1)
                if end < 0:
                    raise RegistryError("Unterminated imported module identifier")
                part = source[index + 1:end]
                if word.fullmatch(part) is None:
                    raise RegistryError("Imported modules require portable ASCII identifier components")
                index = end + 1
            else:
                match = word.match(source, index)
                if match is None:
                    raise RegistryError("Imported modules require portable ASCII identifier components")
                part, index = match.group(), match.end()
            parts.append(part)
            if index < size and source[index] == ".":
                index += 1
                continue
            if index < size and not (source[index].isspace() or source.startswith(("--", "/-"), index)):
                raise RegistryError("Unsupported imported module spelling")
            return lean_name(".".join(parts)), space(index)

    index = space(0)
    module = keyword(index, "module")
    if module is not None:
        index = module
    prelude = keyword(index, "prelude")
    if prelude is not None:
        index = prelude
    imports = set() if prelude is not None else {"Init"}
    while index < size:
        start = index
        public = keyword(index, "public")
        if public is not None:
            index = public
        meta = keyword(index, "meta")
        if meta is not None:
            index = meta
        imported = keyword(index, "import")
        if imported is None:
            # Everything after the header is checked by the registered compiler;
            # body commands cannot extend Lean's fixed module import header.
            private = keyword(start, "private")
            if private is not None and keyword(private, "import") is not None:
                raise RegistryError("Unsupported private import header")
            break
        index = imported
        all_import = keyword(index, "all")
        if all_import is not None:
            index = all_import
        if (public is not None or meta is not None or all_import is not None) and module is None:
            raise RegistryError("Import modifiers require the Lean module header")
        if public is not None and all_import is not None:
            raise RegistryError("Lean does not permit public import all")
        name, index = identifier(index)
        imports.add(name)
    return imports


def source_module_graph(blobs, entries: list[dict[str, str]]) -> tuple[dict, dict[str, set[str]]]:
    """Exact portable project modules, with conservative whole-module imports."""
    modules = {}
    for item in entries:
        if item["path"].endswith(".lean"):
            name = lean_name(module_name(item["path"]))
            if module_path(name) != item["path"]:
                raise RegistryError("Source module paths must use canonical directory components")
            modules[name] = item
    spellings = {name.casefold(): name for name in modules}
    dependencies = {}
    for name, entry in modules.items():
        imports = module_imports(blobs.get(entry["sha256"]).decode("utf-8-sig"))
        if any(imported.casefold() in spellings and spellings[imported.casefold()] != imported for imported in imports):
            raise RegistryError("Project module imports must use the exact source path case")
        dependencies[name] = imports & modules.keys()
    return modules, dependencies


def source_closure(blobs, entries: list[dict[str, str]], roots: list[str]) -> list[dict[str, str]]:
    """Bind same-stem source pairs for roots and their transitive project imports.

    External imports are bound by the immutable environment/toolchain manifests.
    Roots are repository Lean paths chosen by the backend, never a worker-authored list.
    """
    files = {item["path"]: item["sha256"] for item in entries}
    modules, dependencies = source_module_graph(blobs, entries)
    pending = [lean_name(module_name(path)) for path in roots if path.endswith(".lean")]
    if not pending:
        raise RegistryError("A source identity requires at least one Lean module")
    required: set[str] = set()
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in modules:
            raise RegistryError("A bound source module is missing: " + name)
        path = modules[name]["path"]
        for pair_path in (path, path[:-5] + ".md"):
            if pair_path not in files:
                raise RegistryError("A bound module lacks its paired source: " + pair_path)
            required.add(pair_path)
        pending.extend(dependencies[name])
    return [{"path": path, "sha256": files[path]} for path in sorted(required)]


def snapshot_module_order(blobs, entries: list[dict[str, str]]) -> list[str]:
    """Derive a dependency-first order covering every supplied Lean module."""
    modules, dependencies = source_module_graph(blobs, entries)
    result: list[str] = []
    active: set[str] = set()
    completed: set[str] = set()

    for name in sorted(modules):
        pending = [(name, False)]
        while pending:
            current, expanded = pending.pop()
            if expanded:
                active.remove(current)
                completed.add(current)
                result.append(current)
            elif current not in completed:
                if current in active:
                    raise RegistryError("Snapshot Lean import graph contains a cycle")
                active.add(current)
                pending.append((current, True))
                pending.extend((child, False) for child in sorted(dependencies[current], reverse=True))
    return result
