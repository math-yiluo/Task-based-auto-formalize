"""Hourly counts of the latest materialized public Library, never worker trees.

The count is saved under `.task-based`, so a restart of TaskHub or a new dashboard serves the
last count at once instead of reading every published Lean file again."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .content import FileReference, _hash, _path, _paths, _read, _sha
from .storage import atomic_json

SAVED_COUNT = Path(".task-based") / "library-metrics.json"


def seconds_to_next_hour(now: float) -> float:
    """Seconds from `now` to the next local hour on the clock."""
    moment = datetime.fromtimestamp(now)
    following = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(0.0, following.timestamp() - now)


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class LibraryMetrics:
    def __init__(self, project_root: Path, *, clock=time.time):
        self.root = project_root / "library"
        self.saved = project_root / SAVED_COUNT
        self.clock = clock
        self._lock = threading.Lock()
        self._key: tuple[str, str] | None = None
        self._counts: dict[str, int] = {}
        self._result: dict | None = None
        self._error = ""
        # What `latest` serves, replaced whole, so reading it never waits for a count in progress.
        self._snapshot: dict | None = None
        self._load()

    def latest(self) -> dict | None:
        """The last count, with `error` when a later count failed; None before the first count.
        Never reads the Library or waits for a count in progress."""
        snapshot = self._snapshot
        return dict(snapshot) if snapshot is not None else None

    @property
    def error(self) -> str:
        """Why the last count failed; empty after a successful one."""
        return self._error

    def refresh(self) -> dict | None:
        """Count now, keeping the previous count (marked with the error) if this one fails."""
        try:
            return self.count()
        except (OSError, ValueError, KeyError, TypeError) as error:
            with self._lock:
                self._error = str(error) or type(error).__name__
                if self._result is not None:
                    self._snapshot = {**self._result, "error": self._error}
            return None

    def count(self) -> dict:
        # Only the small navigation file is read on an unchanged commit. Counts
        # are observational, not acceptance or integrity evidence for workers.
        with self._lock:
            try:
                current = json.loads(_read(_path(self.root, "current.json")))
            except FileNotFoundError:
                self._counts = {}
                return self._save({"commit": None, "lean_files": 0, "lean_lines": 0}, None)
            if not isinstance(current, dict):
                raise ValueError("Invalid Library navigation")
            commit = current.get("commit")
            if (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
                    or current.get("schema_version") != 1
                    or current.get("root") != f"{commit}/files"):
                raise ValueError("Invalid Library navigation")
            manifest_sha = _hash(current["manifest_sha256"])
            key = (commit, manifest_sha)
            if self._key == key and self._result is not None:
                unchanged = {name: value for name, value in self._result.items() if name != "counted_at"}
                return self._save(unchanged, key)
            body = _read(_path(self.root, f"{commit}/manifest.json"))
            if _sha(body) != manifest_sha:
                raise ValueError("Library manifest does not match the current snapshot")
            manifest = json.loads(body)
            if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                    or manifest.get("kind") != "public" or manifest.get("commit") != commit
                    or not isinstance(manifest.get("files"), list)):
                raise ValueError("Invalid public Library manifest")
            references = [FileReference.from_dict(item) for item in manifest["files"]]
            _paths([ref.path for ref in references])
            counts: dict[str, int] = {}
            total = files = 0
            for ref in references:
                if not ref.path.endswith(".lean"):
                    continue
                if ref.commit != commit:
                    raise ValueError("Library source names a different commit")
                lines = counts.get(ref.sha256, self._counts.get(ref.sha256))
                if lines is None:
                    source = _read(_path(self.root, f"{commit}/files/{ref.path}"))
                    if len(source) != ref.size or _sha(source) != ref.sha256:
                        raise ValueError("Library source does not match its published manifest")
                    # Physical lines, including comments and blanks. CRLF and a
                    # final unterminated line count once; an empty file is zero.
                    lines = len(source.splitlines())
                counts[ref.sha256] = lines
                files += 1
                total += lines
            self._counts = counts
            return self._save({"commit": commit, "lean_files": files, "lean_lines": total}, key)

    def _save(self, result: dict, key: tuple[str, str] | None) -> dict:
        """Record a successful count (the caller holds the lock) and write it, with each source's
        line count by content hash, for the next start. A failed write costs only that start a
        full count."""
        self._key, self._error = key, ""
        self._result = {**result, "counted_at": int(self.clock() * 1000)}
        self._snapshot = dict(self._result)
        if key is not None:  # An unpublished Library costs nothing to count again.
            try:
                atomic_json(self.saved, {**self._result, "schema_version": 1,
                                         "manifest_sha256": key[1], "line_counts": self._counts})
            except OSError:
                pass
        return dict(self._result)

    def _load(self) -> None:
        """Take up the saved count; an unreadable or invalid file leaves the first count to do."""
        try:
            saved = json.loads(self.saved.read_bytes())
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict) or saved.get("schema_version") != 1:
            return
        commit, manifest_sha, counts = saved.get("commit"), saved.get("manifest_sha256"), saved.get("line_counts")
        files, lines, counted_at = (saved.get(name) for name in ("lean_files", "lean_lines", "counted_at"))
        if (not all(_count(value) for value in (files, lines, counted_at)) or not isinstance(counts, dict)
                or not all(isinstance(sha, str) and _count(value) for sha, value in counts.items())):
            return
        if commit is None and manifest_sha is None:
            key = None
        elif (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)
              and isinstance(manifest_sha, str) and re.fullmatch(r"[0-9a-f]{64}", manifest_sha)):
            key = (commit, manifest_sha)
        else:
            return
        self._key, self._counts = key, counts
        self._result = {"commit": commit, "lean_files": files, "lean_lines": lines, "counted_at": counted_at}
        self._snapshot = dict(self._result)
