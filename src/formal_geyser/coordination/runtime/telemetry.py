"""Sanitized activity markers for supervision, without prompts or tool content."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_LOG_LOCKS: dict[str, Any] = {}
_LOG_LOCKS_GUARD = threading.Lock()


class ActivityLog:
    fields = frozenset({
        "run_id", "worker_id", "task_id", "attempt_id", "thread_id", "turn_id", "tool",
        "status", "directive", "method", "error_type", "total_tokens", "process_quiet",
        "diagnostic_sha256",
        "helper_id", "helper_kind", "source_worker_id",
    })

    def __init__(self, state_dir: Path, *, max_bytes: int = 16 * 1024 * 1024, backups: int = 4):
        self.path = state_dir / "runtime-events.jsonl"
        self.max_bytes, self.backups = max_bytes, backups
        if max_bytes < 1 or backups < 1:
            raise ValueError("Activity-log retention must be positive")
        # Several runners can share one service; rotation uses the same path lock.
        with _LOG_LOCKS_GUARD:
            self._lock = _LOG_LOCKS.setdefault(str(self.path.resolve()), threading.Lock())
        self._failing: str | None = None

    async def emit(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event if re.fullmatch(r"[a-z_]{1,64}", event) else "runtime_event",
        }
        for key, value in fields.items():
            if key not in self.fields:
                continue
            if type(value) in {int, bool} or value is None:
                record[key] = value
            elif isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value):
                record[key] = value
        try:
            await asyncio.to_thread(self._append, record)
        except (OSError, ValueError) as error:
            # Monitoring markers never gate execution; report each failure streak once.
            if self._failing != type(error).__name__:
                logger.warning("Runtime activity log is not writable: %s", type(error).__name__)
            self._failing = type(error).__name__
        else:
            self._failing = None

    def _rotate(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        temporary = self.path.with_name(self.path.name + ".rotating")
        try:
            # Finish the compressed copy before touching the active log.
            with self.path.open("rb") as source, temporary.open("wb") as output:
                with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                    shutil.copyfileobj(source, compressed)
                output.flush()
                os.fsync(output.fileno())
            for index in range(self.backups, 1, -1):
                older = self.path.with_name(f"{self.path.name}.{index - 1}.gz")
                if older.exists():
                    older.replace(self.path.with_name(f"{self.path.name}.{index}.gz"))
            temporary.replace(self.path.with_name(self.path.name + ".1.gz"))
            self.path.write_bytes(b"")
        finally:
            temporary.unlink(missing_ok=True)

    def _append(self, record: dict[str, Any]) -> None:
        body = (json.dumps(record, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            with self.path.open("ab") as stream:
                stream.write(body)
                stream.flush()

