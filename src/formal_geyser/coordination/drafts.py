"""Public, unreviewed Lean/Markdown drafts, independent of publication snapshots.

Bodies are immutable native files. The ledger keeps only the current revision's
identity; current.json is rebuildable navigation, never an acceptance receipt.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

import portalocker

from .content import (
    ContentError,
    FileReference,
    NativeContentStore,
    _directory,
    _handle,
    _hash,
    _identity,
    _normal_path,
    _path,
    _paths,
    _plain,
    _read,
    _sha,
)
from .draft_paths import DRAFT_DIRECTORY
from .snapshot_limits import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES
from .storage import canonical, digest


@dataclass(frozen=True)
class PreparedDraftUpdate:
    reference: dict
    task_revision: int


class DraftFiles:
    def __init__(self, content: NativeContentStore):
        self.content = content
        self.root = _path(content.project_root, DRAFT_DIRECTORY)

    def ensure(self) -> Path:
        return _directory(self.root)

    @staticmethod
    def capture(workspace: Path, source_dir: str) -> dict[str, bytes]:
        """Freeze only explicitly shared scratch files; do not scan private notes."""
        from ..mathematical_artifacts import lean_tokens

        if (not isinstance(source_dir, str)
                or not (source_dir == ".private/drafts" or source_dir.startswith(".private/drafts/"))):
            raise ContentError("Draft source_dir must be workspace-relative .private/drafts or a child directory")
        source = _path(workspace, source_dir, private=True)
        if not _normal_path(source).is_dir():
            raise ContentError("Draft source directory does not exist")
        files: dict[str, bytes] = {}
        total = 0
        for current, directories, names in os.walk(_normal_path(source), followlinks=False):
            for name in directories:
                _plain(Path(current) / name)
            for name in sorted(names):
                target = _plain(Path(current) / name)
                relative = target.relative_to(source).as_posix()
                if not relative.endswith((".lean", ".md")):
                    raise ContentError("Public drafts contain only Lean and Markdown files")
                if len(files) >= MAX_SNAPSHOT_FILES or total + _normal_path(target).stat().st_size > MAX_SNAPSHOT_BYTES:
                    raise ContentError("Draft files exceed the native file envelope")
                body = _read(target)
                total += len(body)
                if total > MAX_SNAPSHOT_BYTES:
                    raise ContentError("Draft files exceed the native byte envelope")
                try:
                    text = body.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ContentError("Public drafts must be UTF-8 Lean and Markdown") from exc
                if not text.strip():
                    raise ContentError("Public draft files must be nonempty")
                if relative.endswith(".lean"):
                    holes = {"sorry", "admit", "sorryAx", "axiom"}.intersection(lean_tokens(text))
                    if holes:
                        raise ContentError("Drafts may reference missing definitions, but must not introduce proof holes or axioms")
                files[relative] = body
        _paths(list(files))
        lean = [name for name in files if name.endswith(".lean")]
        if not lean or any(name[:-5] + ".md" not in files for name in lean):
            raise ContentError("Public drafts require at least one Lean file and nonempty same-stem Markdown for every Lean file")
        return files

    def reference(self, task_id: str, revision: int, content_key: str,
                  manifest_sha256: str) -> dict:
        _identity(task_id)
        _hash(content_key)
        _hash(manifest_sha256)
        if type(revision) is not int or revision < 1:
            raise ContentError("Invalid public draft revision")
        directory = _path(self.root, f"{task_id}/revision-{revision}-{content_key}")
        return {"task_id": task_id, "revision": revision, "content_key": content_key,
                "root": str(directory / "files"), "manifest_path": str(directory / "manifest.json"),
                "manifest_sha256": manifest_sha256, "status": "unreviewed",
                "current_path": str(_path(self.root, f"{task_id}/current.json"))}

    def install(self, task_id: str, revision: int, task_revision: int,
                worker_id: str, worker_name: str, summary: str, files: dict[str, bytes]) -> dict:
        metadata = {"kind": "public-draft", "status": "unreviewed", "task_id": task_id,
                    "revision": revision, "task_revision": task_revision,
                    "author_id": worker_id, "author": worker_name, "summary": summary}
        key = digest({**metadata, "files": {name: _sha(body) for name, body in files.items()}})
        _identity(task_id)
        with self._installation_lock():
            view = self.content._tree(
                _path(self.root, f"{task_id}/revision-{revision}-{key}"), files, metadata)
        return self.reference(task_id, revision, key, view.manifest_sha256)

    @contextmanager
    def _installation_lock(self):
        # Immutable draft creation must not block task readers on the content mutex.
        _directory(self.content.state_dir)
        lock = _path(self.content.state_dir, "draft-install.lock")
        with portalocker.Lock(str(_normal_path(lock)), mode="a+b", timeout=30) as stream:
            _handle(cast(BinaryIO, stream), lock)
            if os.fstat(stream.fileno()).st_size:
                raise ContentError("Draft installation lock contains unexpected data")
            yield

    def _manifest(self, reference: dict) -> tuple[FileReference, ...]:
        body = _read(Path(reference["manifest_path"]))
        if _sha(body) != reference["manifest_sha256"]:
            raise ContentError("Public draft manifest differs from its recorded identity")
        manifest = json.loads(body)
        if (manifest.get("kind") != "public-draft" or manifest.get("status") != "unreviewed"
                or manifest.get("task_id") != reference["task_id"]
                or manifest.get("revision") != reference["revision"]):
            raise ContentError("Public draft manifest has a different identity")
        return tuple(FileReference.from_dict(item) for item in manifest["files"])

    def verify(self, reference: dict) -> None:
        self.content._verify_tree(Path(reference["root"]), self._manifest(reference))

    def point(self, reference: dict, *, verified: bool = False) -> None:
        """Replace navigation under the serialized ledger head check.

        Runtime callers verify immutable bodies off the DB executor first. Startup
        may verify here, before any new execution can be admitted.
        """
        reference = self.reference(reference["task_id"], reference["revision"],
                                   reference["content_key"], reference["manifest_sha256"])
        if not verified:
            self.verify(reference)
        else:
            self._manifest(reference)
        encoded = (canonical(reference) + "\n").encode("utf-8")
        path = _path(self.root, f"{reference['task_id']}/current.json")
        previous = _read(path) if _normal_path(path).exists() else None
        if previous != encoded:
            self.content._replace_file(self.root, f"{reference['task_id']}/current.json", encoded,
                                       _sha(previous) if previous is not None else None)
