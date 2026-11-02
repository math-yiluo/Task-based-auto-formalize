"""Exact native files for delivery payloads.

These files retain exact per-worker delivery bytes. Keep referenced files with
worker history and backups; only regenerate them from known matching bytes.
Task/source files and verification records keep their existing ownership.
Never follow hashes found in arbitrary user-authored payloads.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from .content import PRIVATE_DIRECTORY, _directory, _hash, _normal_path, _path, _read, _write_new
from .storage import canonical

MATERIAL_DIRECTORY = f"{PRIVATE_DIRECTORY}/material"


class MaterialFiles:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)

    @staticmethod
    def reference(body: bytes) -> dict:
        """The workspace-relative reference write() returns for these bytes, without writing."""
        body.decode("utf-8")
        sha = hashlib.sha256(body).hexdigest()
        return {"material_path": MaterialFiles.path_for(sha), "material_sha256": sha,
                "material_bytes": len(body), "requires_read": True}

    @staticmethod
    def path_for(sha: str) -> str:
        return f"{MATERIAL_DIRECTORY}/{_hash(sha)}.json"

    def write(self, body: bytes) -> dict:
        """Atomically install exact UTF-8 bytes and return a workspace-relative path."""
        reference = self.reference(body)
        relative = reference["material_path"]
        target = _path(self.workspace, relative, private=True)
        if _normal_path(target).is_file() and _read(target) == body:
            return reference
        _directory(target.parent)
        temporary = _path(self.workspace, f"{MATERIAL_DIRECTORY}/.write-{uuid4().hex}", private=True)
        try:
            _write_new(temporary, body)
            checked = _path(self.workspace, relative, private=True)
            if checked != target:
                raise ValueError("Material destination changed during write")
            os.replace(_normal_path(temporary), _normal_path(target))
        finally:
            if _normal_path(temporary).exists():
                _normal_path(temporary).unlink()
        return reference

    def json(self, value) -> dict:
        return self.write(canonical(value).encode("utf-8"))

    def retained(self, blobs, sha: str) -> dict:
        """Use only a reference produced by a trusted coordination record."""
        return self.write(blobs.get(sha))


class DeferredMaterial(MaterialFiles):
    """Material references made where no file may be written (inside a coordination
    transaction, or on the event loop), for files written afterwards by `flush`, off both. A
    reference is content-addressed, so it is the one write() returns. In the pilot a review's
    entry wrote its files inside its transaction, holding the coordination thread for seconds."""

    def __init__(self, workspace: Path):
        super().__init__(workspace)
        self.pending: list[bytes] = []

    def write(self, body: bytes) -> dict:
        reference = self.reference(body)
        self.pending.append(body)
        return reference

    def flush(self) -> None:
        files = MaterialFiles(self.workspace)
        while self.pending:
            files.write(self.pending.pop(0))
