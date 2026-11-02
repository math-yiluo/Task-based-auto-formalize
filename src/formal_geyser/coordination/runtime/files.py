"""Scoped file access for workers; resource trees expose no write operation."""
from __future__ import annotations

import codecs
import hashlib
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Sequence
from weakref import WeakValueDictionary

from ..content import _normal_path
from ..snapshot_limits import MAX_SNAPSHOT_BYTES
from .tools import _function

_CHUNK_BYTES = 64 * 1024
_INSERT_BYTES = 1024 * 1024
_WRITE_LOCKS: WeakValueDictionary[Path, threading.Lock] = WeakValueDictionary()
_WRITE_LOCKS_GUARD = threading.Lock()


@contextmanager
def _writing(target: Path) -> Iterator[None]:
    # Separate tool calls create separate ScopedFiles instances. Serialize their
    # compare-and-replace operations without retaining a lock for every old path.
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.setdefault(target, threading.Lock())
    with lock:
        yield


def _expected_hash(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
        raise ValueError("Expected SHA-256 must be a lowercase hexadecimal content hash")


def _integer(value: Any, label: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid {label}")
    return value


class ScopedFiles:
    def __init__(self, root: Path):
        root = Path(root)
        if root.is_symlink():
            raise ValueError("Scoped roots cannot be symbolic links")
        self.root = _normal_path(root.resolve(strict=True))
        if not self.root.is_dir():
            raise ValueError("A scoped root must be a directory")

    def path(self, relative: str, *, must_exist: bool = True) -> Path:
        if not isinstance(relative, str) or "\x00" in relative:
            raise ValueError("Path must be a relative string")
        rel = Path(relative)
        if rel.is_absolute() or rel.root or rel.drive or ".." in rel.parts or ":" in relative:
            raise ValueError("Path must stay within its declared root")
        current = self.root
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Symbolic links are not accessible")
        resolved = _normal_path(current.resolve(strict=must_exist))
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path escapes its declared root")
        return resolved

    def _verify_handle(self, stream: BinaryIO, expected: Path) -> None:
        actual = Path(os.readlink(f"/proc/self/fd/{stream.fileno()}")).resolve()
        if not actual.is_relative_to(self.root):
            raise ValueError("Opened file escapes its declared root")

    def list(self, path: str = "", cursor: int = 0, limit: int = 50) -> dict[str, Any]:
        cursor = _integer(cursor, "cursor", 0)
        limit = _integer(limit, "listing limit", 1, 100)
        directory = self.path(path)
        if not directory.is_dir():
            raise ValueError("Listing path must be a directory")
        entries = sorted((_normal_path(item) for item in directory.iterdir()),
                         key=lambda item: (item.name.casefold(), item.name))
        result = []
        for item in entries[cursor:cursor + limit]:
            linked = item.is_symlink()
            result.append({
                "path": item.relative_to(self.root).as_posix(),
                "kind": "blocked_link" if linked else "directory" if item.is_dir() else "file",
                "bytes": None if linked or item.is_dir() else item.stat().st_size,
            })
        return {"path": path, "entries": result, "next_cursor": cursor + len(result),
                "has_more": cursor + len(result) < len(entries)}

    def read(self, path: str, offset: int = 0, limit: int = 4096) -> dict[str, Any]:
        offset = _integer(offset, "offset", 0)
        limit = _integer(limit, "read limit", 1, 16384)
        target = self.path(path)
        decoder = codecs.getincrementaldecoder("utf-8")()
        position = 0
        fragments = []

        def segment(text: str) -> None:
            nonlocal position
            end = position + len(text)
            if end > offset and position < offset + limit:
                fragments.append(text[max(0, offset - position):offset + limit - position])
            position = end

        try:
            source = self._stream_file(target, lambda body: segment(decoder.decode(body)))
            segment(decoder.decode(b"", final=True))
        except UnicodeDecodeError as exc:
            raise ValueError("Resource is not UTF-8 text; use the PDF reader for PDF files") from exc
        fragment = "".join(fragments)
        return {"path": path, "source_sha256": source["sha256"],
                "file_bytes": source["bytes"], "offset": offset, "text": fragment,
                "next_offset": offset + len(fragment), "has_more": offset + len(fragment) < position,
                "offset_unit": "Unicode code points"}

    def _file_state(self, stream: BinaryIO, target: Path) -> tuple[int, int, int, int, int]:
        self._verify_handle(stream, target)
        if self.path(target.relative_to(self.root).as_posix()) != target:
            raise ValueError("File path changed during access")
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not os.path.samestat(info, target.stat()):
            raise ValueError("File changed or is not a regular file")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _stream_file(self, target: Path, consume: Callable[[bytes], None],
                     expected_sha256: str | None = None) -> dict[str, Any]:
        """Read a stable, bounded file without allocating its complete body."""
        hasher = hashlib.sha256()
        total = 0
        with target.open("rb") as stream:
            before = self._file_state(stream, target)
            if before[2] > MAX_SNAPSHOT_BYTES:
                raise ValueError("File exceeds the native file byte limit")
            while body := stream.read(min(_CHUNK_BYTES, MAX_SNAPSHOT_BYTES - total + 1)):
                total += len(body)
                if total > MAX_SNAPSHOT_BYTES:
                    raise ValueError("File exceeds the native file byte limit")
                hasher.update(body)
                consume(body)
            if self._file_state(stream, target) != before or total != before[2]:
                raise ValueError("File changed during access")
        sha = hasher.hexdigest()
        if expected_sha256 is not None and sha != expected_sha256:
            raise ValueError("File changed; read its current SHA-256 before replacing or copying it")
        return {"bytes": total, "sha256": sha}

    def _destination(self, path: str, expected_sha256: str | None,
                     *, require_expected: bool = False) -> tuple[Path, bool]:
        target = self.path(path, must_exist=False)
        if target == self.root:
            raise ValueError("A file path is required")
        exists = target.exists()
        if exists and not target.is_file():
            raise ValueError("Workspace destination must be a regular file")
        if exists and require_expected and expected_sha256 is None:
            raise ValueError("Provide expected_sha256 before replacing an existing workspace file")
        if expected_sha256 is not None:
            if not exists:
                raise ValueError("Expected existing workspace file is missing")
            self._stream_file(target, lambda _: None, expected_sha256)
        return target, exists

    def _atomic_write(self, path: str, target: Path, existed: bool,
                      expected_sha256: str | None,
                      produce: Callable[[Callable[[bytes], None]], None]) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.path(path, must_exist=False) != target:
            raise ValueError("Workspace destination changed during write")
        fd, name = tempfile.mkstemp(prefix=".task-based-write-", dir=target.parent)
        temporary = _normal_path(Path(name))
        hasher = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as stream:
                self._file_state(stream, temporary)

                def append(body: bytes) -> None:
                    nonlocal total
                    if total + len(body) > MAX_SNAPSHOT_BYTES:
                        raise ValueError("Resulting workspace file exceeds the native file byte limit")
                    stream.write(body)
                    hasher.update(body)
                    total += len(body)

                produce(append)
                stream.flush()
                os.fsync(stream.fileno())
                if self._file_state(stream, temporary)[2] != total:
                    raise ValueError("Workspace temporary file changed during write")
            if self.path(path, must_exist=False) != target:
                raise ValueError("Workspace destination changed during write")
            if expected_sha256 is not None:
                # A long streamed edit must not replace a file that changed
                # after its original read. This check also covers native edits.
                self._stream_file(target, lambda _: None, expected_sha256)
            if existed:
                os.replace(temporary, target)
            else:
                # Unlike replace, installation by link cannot clobber a file
                # that appeared while a new destination was being prepared.
                os.link(temporary, target)
                temporary.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()
        return {"path": path, "bytes": total, "sha256": hasher.hexdigest(), "written": True}

    def write(self, path: str, text: str, expected_sha256: str | None = None,
              *, offset: int | None = None, delete_count: int = 0) -> dict[str, Any]:
        """Replace a small file or a Unicode range in an exact existing UTF-8 file."""
        if not isinstance(text, str):
            raise ValueError("Workspace content must be text")
        body = text.encode("utf-8")
        if len(body) > _INSERT_BYTES:
            raise ValueError("One workspace write is limited to 1 MiB of inserted text")
        _expected_hash(expected_sha256)
        delete_count = _integer(delete_count, "delete count", 0)
        if offset is None and delete_count:
            raise ValueError("delete_count requires an offset")
        if offset is not None:
            offset = _integer(offset, "offset", 0)
            if expected_sha256 is None:
                raise ValueError("Partial edits require expected_sha256")
        with _writing(self.path(path, must_exist=False)):
            target, existed = self._destination(path, expected_sha256)
            if offset is None:
                return self._atomic_write(path, target, existed, expected_sha256,
                                          lambda append: append(body))

            def produce(append: Callable[[bytes], None]) -> None:
                decoder = codecs.getincrementaldecoder("utf-8")()
                position = 0
                inserted = False
                end_delete = offset + delete_count

                def segment(text: str) -> None:
                    nonlocal position, inserted
                    end = position + len(text)
                    if not inserted and offset <= end:
                        append(text[:offset - position].encode("utf-8"))
                        append(body)
                        inserted = True
                    if inserted:
                        append(text[max(0, end_delete - position):].encode("utf-8"))
                    else:
                        append(text.encode("utf-8"))
                    position = end

                self._stream_file(target, lambda chunk: segment(decoder.decode(chunk)),
                                  expected_sha256)
                segment(decoder.decode(b"", final=True))
                if not inserted or end_delete > position:
                    raise ValueError("Partial edit range exceeds the file's Unicode character length")

            result = self._atomic_write(path, target, existed, expected_sha256, produce)
            return {**result, "source_sha256": expected_sha256, "offset": offset,
                    "delete_count": delete_count, "offset_unit": "Unicode code points"}

    def copy_from(self, source: ScopedFiles, source_path: str, path: str,
                  *, expected_source_sha256: str | None = None,
                  expected_sha256: str | None = None) -> dict[str, Any]:
        """Copy a bounded source file into this workspace without altering its source."""
        _expected_hash(expected_source_sha256)
        _expected_hash(expected_sha256)
        source_target = source.path(source_path)
        with _writing(self.path(path, must_exist=False)):
            target, existed = self._destination(path, expected_sha256, require_expected=True)
            captured: dict[str, Any] = {}

            def produce(append: Callable[[bytes], None]) -> None:
                captured.update(source._stream_file(source_target, append, expected_source_sha256))

            result = self._atomic_write(path, target, existed, expected_sha256, produce)
            return {**result, "source_path": source_path, "source_sha256": captured["sha256"]}

    def _files(self, path: str) -> Sequence[Path]:
        base = self.path(path)
        if base.is_file():
            return [base]
        files = []
        for current, directories, names in os.walk(base, followlinks=False):
            directories[:] = [
                name for name in directories
                if name not in {".git", ".venv", ".lake", "__pycache__", "node_modules",
                                ".mypy_cache", ".ruff_cache", ".pytest_cache"}
                and not (Path(current) / name).is_symlink()
            ]
            for name in names:
                item = Path(current) / name
                if item.is_symlink():
                    continue
                files.append(item)
                if len(files) > 50000:
                    raise ValueError("Search scope exceeds 50000 files; choose a narrower path")
        return sorted(files, key=lambda item: item.relative_to(self.root).as_posix().casefold())

    def search(
        self, query: str, path: str = "", mode: str = "path", cursor: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query or len(query) > 1000:
            raise ValueError("Search query must contain 1 to 1000 characters")
        if mode not in {"path", "text"}:
            raise ValueError("Search mode must be path or text")
        cursor = _integer(cursor, "cursor", 0)
        limit = _integer(limit, "search limit", 1, 50)
        files = self._files(path)
        matches = []
        scanned = skipped = 0
        index = cursor
        deadline = time.monotonic() + 3
        needle = query.casefold()
        while index < len(files) and scanned < 256 and time.monotonic() < deadline:
            item = files[index]
            index += 1
            scanned += 1
            relative = item.relative_to(self.root).as_posix()
            if mode == "path":
                if needle in relative.casefold():
                    matches.append({"path": relative, "bytes": item.stat().st_size})
            else:
                target = self.path(relative)
                with target.open("rb") as stream:
                    self._verify_handle(stream, target)
                    body = stream.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024 or b"\x00" in body:
                    skipped += 1
                    continue
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                for line_number, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        matches.append({"path": relative, "line": line_number,
                                        "excerpt": line[:300], "excerpt_truncated": len(line) > 300})
                        break
            if len(matches) >= limit:
                break
        return {"matches": matches, "next_cursor": index, "has_more": index < len(files),
                "scanned_files": scanned, "skipped_binary_or_large_files": skipped}

    def pdf(self, path: str, page: int = 1, count: int = 1, *, render: Path | None = None) -> dict[str, Any]:
        """The text of up to three pages; with `render`, also each page as a grayscale PNG image
        written into that directory (`images`, with absolute paths)."""
        page = _integer(page, "PDF page", 1)
        count = _integer(count, "PDF page count", 1, 3)
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF reading requires the installed pypdf dependency in the Hub runtime") from exc
        target = self.path(path)
        with target.open("rb") as stream:
            self._verify_handle(stream, target)
            if os.fstat(stream.fileno()).st_size > 256 * 1024 * 1024:
                raise ValueError("PDF exceeds the 256 MiB reader limit")
            reader = PdfReader(stream, strict=False)
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported by this reader")
            total = len(reader.pages)
            if page > total:
                raise ValueError("PDF page is out of range")
            end = min(page - 1 + count, total)
            pages = [{"page": index + 1, "text": reader.pages[index].extract_text() or ""}
                     for index in range(page - 1, end)]
            images = None
            if render is not None:
                from .pdf_render import render as render_pages

                info = os.fstat(stream.fileno())
                key = hashlib.sha256(f"{target}\0{info.st_size}\0{info.st_mtime_ns}".encode()).hexdigest()[:16]
                images = [{"page": item["page"], "path": str(render / item["name"]),
                           "width": item["width"], "height": item["height"]}
                          for item in render_pages(stream, render, key, list(range(page, end + 1)))]
        result = {"path": path, "pages": pages, "total_pages": total, "next_page": end + 1, "has_more": end < total}
        if images is not None:
            result["images"] = images
        return result


class ResourceRoots:
    def __init__(self, roots: Any = None):
        if roots is None:
            roots = {}
        if not isinstance(roots, dict) or len(roots) > 32:
            raise ValueError("environment.resource_roots must map at most 32 names to paths")
        self.roots: dict[str, ScopedFiles] = {}
        for name, value in roots.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError("Resource root names must be short ASCII identifiers")
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("Resource roots must be absolute directory paths")
            self.roots[name] = ScopedFiles(Path(value))

    def get(self, name: str) -> ScopedFiles:
        if name not in self.roots:
            raise ValueError("Unknown resource root. Available roots: " + ", ".join(sorted(self.roots)))
        return self.roots[name]



def workspace_tools() -> list[dict[str, Any]]:
    path = {"type": "string"}
    offset = {"type": "integer", "minimum": 0}
    return [
        _function("taskhub_workspace_list", "List your workspace or an absolute project/declared-resource directory.",
                  {"path": path, "cursor": offset, "limit": {"type": "integer", "minimum": 1,
                                                            "maximum": 100}}, []),
        _function("taskhub_workspace_read", "Read a UTF-8 file in your workspace, the project or a declared resource; absolute paths are accepted.",
                  {"path": path, "offset": offset, "limit": {"type": "integer", "minimum": 1,
                                                            "maximum": 16384}}, ["path"]),
        _function("taskhub_workspace_write",
                  "Atomically write UTF-8 text inside your workspace, up to 1 MiB per call. "
                  "For a local edit in a larger file, supply offset and delete_count in Unicode characters "
                  "and the required expected_sha256; text replaces that range. Omit offset for a full write.",
                  {"path": path, "text": {"type": "string"},
                   "offset": offset, "delete_count": offset,
                   "expected_sha256": {"type": "string"}}, ["path", "text"]),
        _function("taskhub_workspace_copy",
                  "Copy a file from a declared read-only resource into your workspace without passing its "
                  "contents through the model. Pin the source with expected_source_sha256 if known. "
                  "Replacing an existing destination requires its expected_sha256.",
                  {"root": path, "source_path": path, "path": path,
                   "expected_source_sha256": {"type": "string"},
                   "expected_sha256": {"type": "string"}}, ["root", "source_path", "path"]),
    ]


def resource_tools() -> list[dict[str, Any]]:
    path = {"type": "string"}
    offset = {"type": "integer", "minimum": 0}
    return [
        _function("taskhub_resource_list", "List a directory within a read-only resource root.",
                  {"root": path, "path": path, "cursor": offset,
                   "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["root"]),
        _function("taskhub_resource_read", "Read a UTF-8 text or source file without copying its tree.",
                  {"root": path, "path": path, "offset": offset,
                   "limit": {"type": "integer", "minimum": 1, "maximum": 16384}}, ["root", "path"]),
        _function("taskhub_resource_search",
                  "Search resource filenames or UTF-8 text. Follow next_cursor while has_more. "
                  "Search skips version-control and generated cache directories. Text search skips binary "
                  "files and files above 2 MiB; excerpts are previews.",
                  {"root": path, "query": {"type": "string"}, "path": path,
                   "mode": {"type": "string", "enum": ["path", "text"]}, "cursor": offset,
                   "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["root", "query"]),
        _function("taskhub_resource_pdf",
                  "Extract text from up to three PDF pages using the included pypdf dependency. "
                  "Page numbers start at one. With render, each page is also saved as a grayscale PNG "
                  "image (about 150 dpi) under .private/tmp/pdf-pages/ in your workspace, for scanned "
                  "pages, formulas and figures: open the returned images with your image viewer.",
                  {"root": path, "path": path, "page": {"type": "integer", "minimum": 1},
                   "count": {"type": "integer", "minimum": 1, "maximum": 3},
                   "render": {"type": "boolean"}}, ["root", "path"]),
    ]

