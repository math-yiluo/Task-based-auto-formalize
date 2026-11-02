"""A browsable literature folder with one index and internal citation receipts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from .content import settled
from .errors import HubError
from .storage import canonical, digest

# File digests, reused while a file's size, modification time and ID are unchanged. Enough for
# every registered source: in the Linux pilot one source (a Lean project) held 60,579 files, so a
# cache of 4,096 was cleared at each reading of it and its 1.2 GB hashed again.
_DIGESTS: dict[str, tuple[tuple[int, int, int], str]] = {}
_DIGESTS_LOCK = threading.Lock()
DIGESTS_KEPT = 262_144

INDEX = "_central-library-pointers.md"
_INDEX_LOCK = threading.RLock()
INDEX_HEADER = """# Literature pointers

The read-only central collection, when the project has one, is `central_literature`.
Local documents and publication folders sit beside this file under readable names.
See the project's `wiki/literature.md` for usage. Registration appends citation
entries here; keep reading notes in your private or public notes.
"""


def _file_digest(target: Path) -> str:
    info = os.stat(target)
    identity = (info.st_size, info.st_mtime_ns, info.st_ino)
    key = os.path.abspath(target)
    reusable = settled(info.st_mtime_ns)
    if reusable:
        with _DIGESTS_LOCK:
            cached = _DIGESTS.get(key)
        if cached is not None and cached[0] == identity:
            return cached[1]
    with target.open("rb") as stream:
        value = hashlib.file_digest(stream, "sha256").hexdigest()
    if reusable:
        with _DIGESTS_LOCK:
            if len(_DIGESTS) >= DIGESTS_KEPT:
                _DIGESTS.clear()
            _DIGESTS[key] = (identity, value)
    return value


def fingerprint(path: Path) -> dict:
    """Keep existing file hashes; bind a folder to every relative filename and byte.

    A file is hashed again only when its size, modification time or ID changed (a large PDF
    need not be re-read).
    """
    if path.is_file():
        return {"source_kind": "file", "source_sha256": _file_digest(path)}
    if not path.is_dir():
        raise HubError("SOURCE_UNAVAILABLE", "Literature must be a readable file or publication folder", 422)
    def unreadable(error):
        raise HubError("SOURCE_UNAVAILABLE", "The complete publication folder must be readable", 422) from error

    files = {}
    for base, directories, names in os.walk(path, followlinks=False, onerror=unreadable):
        for name in directories + names:
            child = Path(base) / name
            if child.is_symlink():
                raise HubError("SOURCE_UNAVAILABLE", "A publication folder cannot contain linked paths", 422)
        for name in sorted(names):
            child = Path(base) / name
            if not child.is_file():
                raise HubError("SOURCE_UNAVAILABLE", "A publication folder must contain regular files", 422)
            files[child.relative_to(path).as_posix()] = fingerprint(child)["source_sha256"]
    if not files:
        raise HubError("SOURCE_UNAVAILABLE", "A publication folder must contain source files", 422)
    return {"source_kind": "directory", "source_sha256": digest({"files": files}), "files": files}


class LiteratureFiles:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / "literature"

    def owned(self, relative: str) -> Path:
        path = self.root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise HubError("CONTENT_PATH_INVALID", "Literature metadata must stay within the project")
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise HubError("CONTENT_PATH_INVALID", "Owned literature paths must not be links")
        return path

    def write(self, path: Path, body: bytes, *, immutable: bool = False) -> str:
        target = self.owned(path.relative_to(self.root).as_posix())
        if immutable and target.exists():
            if target.read_bytes() != body:
                raise HubError("SOURCE_VERSION_CONFLICT", "Register changed literature under a new source ID")
            return hashlib.sha256(body).hexdigest()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(body).hexdigest()

    def ensure_index(self) -> Path:
        with _INDEX_LOCK:
            path = self.owned("literature/" + INDEX)
            if not path.exists():
                self.write(path, INDEX_HEADER.encode("utf-8"))
            return path

    def add_pointer(self, record: dict) -> Path:
        # Database transactions serialize registrations; this also excludes guide initialization.
        with _INDEX_LOCK:
            return self._add_pointer(record)

    def _add_pointer(self, record: dict) -> Path:
        path = self.ensure_index()
        existing = path.read_bytes()
        marker = f"<!-- literature:{record['id']} -->"
        if marker.encode("utf-8") in existing:
            return path
        source = Path(record["source_path"])
        relative = source.relative_to(self.directory) if source.is_relative_to(self.directory) else None
        location = relative.as_posix() if relative is not None else source.as_posix()
        title = " ".join(record["title"].splitlines())
        kind = "folder" if record.get("source_kind") == "directory" else "file"
        entry = f"\n\n{marker}\n## {title}\n\n- Source ID: `{record['id']}`\n- [{kind}] `{location}`\n"
        for label, field in (("Edition", "version"), ("Origin", "uri"), ("Citation", "citation")):
            if record.get(field):
                entry += f"- {label}: {' '.join(str(record[field]).splitlines())}\n"
        if record.get("text"):
            entry += "\n" + record["text"] + "\n"
        self.write(path, existing + entry.encode("utf-8"))
        return path

    def retain(self, source: Path, source_id: str, title: str, observed: dict) -> Path:
        if source.is_relative_to(self.directory):
            return source
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title).strip(" .")[:140].rstrip(" .") or source_id
        if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.I):
            name = "Publication - " + name
        extension = source.suffix if observed["source_kind"] == "file" else ""
        if extension and name.lower().endswith(extension.lower()):
            name = name[:-len(extension)]
        for suffix in ("", " -- " + source_id):
            target = self.owned(f"literature/{name}{suffix}{extension}")
            if not target.exists():
                break
            if fingerprint(target) == observed:
                return target
        else:
            raise HubError("SOURCE_VERSION_CONFLICT", "A different local source already uses this publication name")
        temporary = self.owned("literature/.import-" + uuid4().hex)
        temporary.parent.mkdir(parents=True, exist_ok=True)
        try:
            if observed["source_kind"] == "file":
                shutil.copyfile(source, temporary)
            else:
                temporary.mkdir()
                for relative in observed["files"]:
                    destination = temporary / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / relative, destination)
            if fingerprint(source) != observed or fingerprint(temporary) != observed:
                raise HubError("SOURCE_CHANGED", "Literature changed while importing its local copy")
            # Never overwrite a different document, including after a lost retry.
            if target.exists():
                raise HubError("SOURCE_VERSION_CONFLICT", "The local publication destination was occupied")
            os.rename(temporary, target)
            return target
        finally:
            if temporary.exists():
                resolved = temporary.resolve()
                if not resolved.is_relative_to(self.directory.resolve()):
                    raise HubError("CONTENT_PATH_INVALID", "Import cleanup escaped the literature directory")
                if resolved.is_dir():
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink()

    def register(self, source_id: str, payload: dict) -> tuple[str, str, dict]:
        try:
            source = Path(payload["path"]).resolve(strict=True)
        except OSError as error:
            raise HubError("SOURCE_UNAVAILABLE", "The literature source is not accessible", 422) from error
        if source.is_dir() and self.directory.is_relative_to(source):
            raise HubError("SOURCE_UNAVAILABLE", "Register a publication, not the project literature collection", 422)
        observed = fingerprint(source)
        if payload.get("expected_fingerprint", observed) != observed:
            raise HubError("SOURCE_CHANGED", "Literature changed before importing the requested version")
        title = payload["title"]
        if payload.get("copy", False):
            source = self.retain(source, source_id, title, observed)
        stored_path = (source.relative_to(self.root).as_posix()
                       if source.is_relative_to(self.directory) else str(source))
        record = {"id": source_id, "title": title, "source_path": stored_path, **observed,
                  "uri": payload.get("uri"), "version": payload.get("version"),
                  "citation": payload.get("citation", ""), "text": payload.get("description") or title}
        relative = f".task-based/literature/source-{source_id}.json"
        path = self.owned(relative)
        sha = self.write(path, (canonical(record) + "\n").encode("utf-8"), immutable=True)
        visible = {key: value for key, value in record.items() if key != "files"}
        return relative, sha, {**visible, "source_path": str(source), "path": str(path),
                               "index_path": str(self.directory / INDEX)}

    def read(self, row) -> dict:
        path = self.owned(row["path"])
        try:
            body = path.read_bytes()
        except FileNotFoundError as error:
            raise HubError("CONTENT_UNAVAILABLE", "The citation receipt is missing", 503) from error
        if hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise HubError("CONTENT_CHANGED", "The recorded literature version changed")
        record = json.loads(body)
        source = Path(record["source_path"])
        if not source.is_absolute():
            source = self.owned(record["source_path"])
        visible = {key: value for key, value in record.items() if key != "files"}
        return {**visible, "source_path": str(source), "path": str(path),
                "index_path": str(self.directory / INDEX)}

    @staticmethod
    def verify(record: dict) -> None:
        try:
            observed = fingerprint(Path(record["source_path"]))
        except (OSError, HubError) as error:
            raise HubError("SOURCE_UNAVAILABLE", "The exact registered literature is not currently accessible") from error
        if observed["source_sha256"] != record["source_sha256"]:
            raise HubError("SOURCE_UNAVAILABLE", "The exact registered literature has changed")
