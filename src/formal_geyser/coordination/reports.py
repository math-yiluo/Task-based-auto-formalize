"""Exact machine output, stored once; small summaries stay in the ledger.

This is not a validation cache. Each execution still produces its own evidence
identity; only byte-identical report bodies share storage. Writes finish before
the transaction that refers to them. Never collect these files by age.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
from pathlib import Path

from .storage import atomic_write, canonical

INLINE_BYTES = 4096
MAX_REPORT_BYTES = 128 * 1024 * 1024


def processes_quiet(value):
    if isinstance(value, dict):
        return (value.get("tree_quiet") is not False and value.get("process_quiet") is not False
                and all(processes_quiet(item) for item in value.values()))
    return not isinstance(value, list) or all(processes_quiet(item) for item in value)


class ReportStore:
    def __init__(self, state_dir: Path):
        self.root = state_dir / "reports"
        self._lock = threading.RLock()

    def path(self, sha: str):
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            raise ValueError("Invalid report hash")
        return self.root / sha[:2] / (sha + ".json.gz")

    def read(self, sha: str):
        with gzip.open(self.path(sha), "rb") as stream:
            body = stream.read(MAX_REPORT_BYTES + 1)
        if len(body) > MAX_REPORT_BYTES or hashlib.sha256(body).hexdigest() != sha:
            raise ValueError("Report integrity failure")
        return json.loads(body)

    def put(self, value):
        body = canonical(value).encode("utf-8")
        if len(body) > MAX_REPORT_BYTES:
            raise ValueError("Machine report exceeds the retained report limit")
        sha = hashlib.sha256(body).hexdigest()
        path = self.path(sha)
        with self._lock:
            if path.exists():
                self.read(sha)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, gzip.compress(body, compresslevel=6, mtime=0), prefix=".report-")
        return {"sha256": sha, "path": self.path(sha).relative_to(self.root.parent).as_posix(), "format": "json+gzip", "bytes": len(body)}

    def compact(self, details: dict) -> dict:
        if not isinstance(details, dict):
            raise ValueError("Machine report details must be a JSON object")
        if "report_file" in details:
            self.read(details["report_file"]["sha256"])
            return details
        if len(canonical(details).encode("utf-8")) <= INLINE_BYTES:
            return details
        reference = self.put(details)
        # Small scalar findings and exact gate identities remain queryable.
        summary = {key: value for key, value in details.items()
                   if key == "candidate_binding" or len(canonical(value).encode("utf-8")) <= 512}
        if isinstance(details.get("reason"), str):
            summary["reason"] = details["reason"][:2048]
        summary["process_quiet"] = processes_quiet(details)
        summary["report_file"] = reference
        return summary

    def report(self, report: dict) -> dict:
        if not isinstance(report, dict):
            raise ValueError("Machine reports must be JSON objects")
        return {**report, "details": self.compact(report.get("details", {}))}
