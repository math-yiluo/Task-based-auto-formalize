"""Durable native submissions, accepted archives, and recoverable relocation.

An immutable relocation journal precedes the directory rename. SQLite may roll
back after the rename; reads resolve the same ID and exact bytes through that
journal, and the next coordinated operation repairs the stored path. An absent
or modified frozen file is never repaired from a validation blob.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .content import (
    ContentConflict,
    ContentError,
    FileReference,
    FreshTree,
    NativeContentStore,
    _directory,
    _flush,
    _hash,
    _identity,
    _normal_path,
    _path,
    _paths,
    _plain,
    _read,
    _sha,
    _write_new,
    fresh_tree,
    verify_references,
)
from .snapshot_limits import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES
from .storage import canonical, digest

_MAX_ARCHIVE_BYTES = 2 * MAX_SNAPSHOT_BYTES + 64 * 1024 * 1024
_MAX_ARCHIVE_FILES = 2 * MAX_SNAPSHOT_FILES + 32  # Original/final trees and bounded evidence files.
# In a submission archive prepared against its Library base: that base, and what the candidate
# removed from it. `files/` then holds only what differs from the base (and the Lean/Markdown
# pair); every other file of the snapshot is the base's. In the Linux pilot each archive held
# the whole Library, 1,000 to 1,900 files: 17 GB in all, and writing, verifying and moving
# them took a third of the service's CPU.
BASE_FILE = "base.json"


def candidate_delta(blobs, snapshot: str, base_commit: str, base_snapshot: str) -> tuple[list[dict], bytes]:
    """The entries of a candidate's snapshot that differ from its Library base, and the body of
    its `base.json`."""
    base = {item["path"]: item["sha256"] for item in blobs.get_json(base_snapshot)["files"]}
    entries = blobs.get_json(snapshot)["files"]
    record = {"schema_version": 1, "snapshot": snapshot, "base_commit": base_commit,
              "base_snapshot": base_snapshot,
              "removed": sorted(set(base) - {item["path"] for item in entries})}
    return ([item for item in entries if base.get(item["path"]) != item["sha256"]],
            (canonical(record) + "\n").encode("utf-8"))


def _listing(directory: Path) -> dict[str, int]:
    """Relative file names and sizes from directory entries, without opening any file."""
    result: dict[str, int] = {}
    pending = [("", os.fspath(_plain(directory)))]
    while pending:
        prefix, current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ContentError("Linked paths are forbidden in a frozen archive")
                name = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    pending.append((name + "/", entry.path))
                else:
                    result[name] = entry.stat(follow_symlinks=False).st_size
    return result


@dataclass
class PreparedSources:
    """A submission snapshot's files, written and verified before the staging transaction.

    The staging transaction adds only the record, any missing Lean/Markdown pair file and
    the manifest, then renames the tree into place.
    """
    snapshot: str
    directory: Path
    refs: dict[str, FileReference]
    consumed: bool = field(default=False)
    base_commit: str | None = None  # The Library base the files were selected against, if any.

    def take(self) -> Path | None:
        if self.consumed:
            return None
        self.consumed = True
        return self.directory


class ArtifactFiles:
    def __init__(self, content: NativeContentStore):
        self.content = content
        self.root = content.project_root
        self.journals = content.root / "relocations"

    @staticmethod
    def version(data: Mapping[str, Any]) -> int:
        version = data.get("version", 1)
        if type(version) is not int or version < 1:
            raise ContentError("A submission version must be a positive integer")
        return version

    def location(self, artifact_id: str, version: int, area: str) -> Path:
        _identity(artifact_id)
        if type(version) is not int or version < 1:
            raise ContentError("Invalid submission version")
        if area not in {"staging", "artifacts", "submission-history"}:
            raise ContentError("Invalid submission area")
        return _path(self.root, f"{area}/{artifact_id}/{version}")

    def _journal(self, artifact_id: str, version: int, source: Path) -> Path:
        # Each move (staging -> archive or history) has its own permanent receipt, so
        # paths given in earlier prompts remain resolvable.
        relative = source.relative_to(self.root).as_posix()
        key = digest({"artifact_id": artifact_id, "version": version, "source": relative})
        return _path(self.journals, artifact_id + "/" + key + ".json")

    @staticmethod
    def _archive_manifest(files: Mapping[str, bytes], metadata: dict) -> tuple[list[str], bytes]:
        names = _paths(list(files), max_files=_MAX_ARCHIVE_FILES - 1)
        if "manifest.json" in names or sum(len(files[name]) for name in names) > _MAX_ARCHIVE_BYTES:
            raise ContentError("Invalid or oversized artifact archive")
        refs = [FileReference(name, _sha(files[name]), len(files[name])) for name in names]
        return names, canonical({"schema_version": 1, **metadata,
                                 "files": [ref.as_dict() for ref in refs]}).encode("utf-8")

    def _stage_archive(self, destination: Path, files: Mapping[str, bytes], metadata: dict) -> Path | None:
        """Write, flush and verify an archive for `destination` in a fresh folder; None when
        `destination` exists. Needs no lock: nothing else opens a fresh folder."""
        names, manifest = self._archive_manifest(files, metadata)
        if _normal_path(destination).exists():
            return None
        # What verify checks besides the files' bytes, checked before anything is written.
        _identity(metadata["artifact_id"])
        _hash(metadata["body_sha256"])
        if "record.json" not in files or digest(json.loads(files["record.json"])) != metadata["body_sha256"]:
            raise ContentError("Frozen artifact record differs from its identity")
        _directory(destination.parent)
        temporary = fresh_tree(destination.parent)
        try:
            with FreshTree(temporary) as tree:
                for name in names:
                    tree.write(name, files[name])
                tree.write("manifest.json", manifest)
                _flush(tree.written)
                tree.verify([FileReference(name, _sha(files[name]), len(files[name])) for name in names]
                            + [FileReference("manifest.json", _sha(manifest), len(manifest))])
        except BaseException:
            self.content._discard_temporary(temporary)
            raise
        return temporary

    def _install(self, destination: Path, files: Mapping[str, bytes], metadata: dict,
                 *, staged: Path | None = None) -> None:
        """Rename a staged archive (written now when none is given) into `destination`, or verify
        the identical archive already there; a staged copy left over is discarded."""
        _, manifest = self._archive_manifest(files, metadata)
        try:
            if staged is None and not _normal_path(destination).exists():
                staged = self._stage_archive(destination, files, metadata)
            if staged is not None and not _normal_path(destination).exists():
                try:
                    os.rename(_plain(staged), _plain(destination))
                    return
                except OSError:
                    if not _normal_path(destination).exists():
                        raise
            if _read(destination / "manifest.json") != manifest:
                raise ContentConflict("An immutable artifact archive already differs")
            self.verify(destination, metadata["artifact_id"], metadata["version"], metadata["body_sha256"])
        finally:
            if staged is not None and _normal_path(staged).exists():
                self.content._discard_temporary(staged)

    def prepare_sources(self, blobs, snapshot: str, base: tuple[str, str] | None = None) -> PreparedSources:
        """Write and verify a snapshot's files for a later staging transaction (off its thread):
        with the Library `base` (commit, snapshot) the worker started from, only the files that
        differ from it, and `base.json`."""
        extra: dict[str, bytes] = {}
        if base is not None:
            entries, extra[BASE_FILE] = candidate_delta(blobs, snapshot, *base)
        else:
            entries = blobs.get_json(snapshot)["files"]
        names = _paths(["files/" + item["path"] for item in entries] + list(extra),
                       max_files=_MAX_ARCHIVE_FILES - 1)
        area = _path(self.root, "staging")
        _directory(area)
        temporary = fresh_tree(area)
        refs: dict[str, FileReference] = {}
        try:
            total = 0
            with FreshTree(temporary) as tree:
                for item in entries:
                    body = blobs.get(item["sha256"])
                    total += len(body)
                    if total > _MAX_ARCHIVE_BYTES:
                        raise ContentError("Invalid or oversized artifact archive")
                    name = "files/" + item["path"]
                    tree.write(name, body)
                    refs[name] = FileReference(name, item["sha256"], len(body))
                for name, body in extra.items():
                    tree.write(name, body)
                    refs[name] = FileReference(name, _sha(body), len(body))
                if sorted(refs) != names:
                    raise ContentError("Snapshot paths are not unique archive paths")
                _flush(tree.written)
                tree.verify(list(refs.values()))
        except BaseException:
            self.content._discard_temporary(temporary)
            raise
        return PreparedSources(snapshot, temporary, refs, base_commit=base[0] if base is not None else None)

    def discard(self, prepared: PreparedSources) -> None:
        directory = prepared.take()
        if directory is not None and _normal_path(directory).exists():
            self.content._discard_temporary(directory)

    def stage(self, artifact_id: str, data: dict, sources: Mapping[str, bytes],
              prepared: PreparedSources | None = None) -> Path:
        version = self.version(data)
        destination = self.location(artifact_id, version, "staging")
        body = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        files = {"record.json": body, **{"files/" + name: value for name, value in sources.items()}}
        metadata = {"artifact_id": artifact_id, "version": version, "body_sha256": digest(data),
                    "disposition": "staged"}
        if prepared is not None:
            # A new submission's folder is renamed into a fresh, unique location: the content
            # lock, which archive moves and Library views hold for seconds, is not needed.
            self._install_prepared(destination, prepared, files, metadata)
        else:
            with self.content._locked():
                self._install(destination, files, metadata)
        return destination / "record.json"

    def _install_prepared(self, destination: Path, prepared: PreparedSources, files: Mapping[str, bytes],
                          metadata: dict) -> None:
        """Bind prepared source files: write only the record, missing pair files and manifest."""
        refs = dict(prepared.refs)
        added = {}
        for name, body in files.items():
            known = refs.get(name)
            if known is not None:
                if known.sha256 != _sha(body):
                    raise ContentConflict("A prepared source file differs from the frozen submission")
                continue
            refs[name] = FileReference(name, _sha(body), len(body))
            added[name] = body
        names = _paths(list(refs), max_files=_MAX_ARCHIVE_FILES - 1)
        if "manifest.json" in names or sum(refs[name].size for name in names) > _MAX_ARCHIVE_BYTES:
            raise ContentError("Invalid or oversized artifact archive")
        manifest = canonical({"schema_version": 1, **metadata,
                              "files": [refs[name].as_dict() for name in names]}).encode("utf-8")
        if _normal_path(destination).exists():
            if _read(destination / "manifest.json") != manifest:
                raise ContentConflict("An immutable artifact archive already differs")
            self.verify(destination, metadata["artifact_id"], metadata["version"], metadata["body_sha256"])
            return
        temporary = prepared.take()
        if temporary is None:
            raise ContentError("Prepared submission files were already used")
        try:
            # The files were verified when written. Their names and sizes are checked from the
            # directory listing; their bytes are checked again when read or when the archive moves.
            if _listing(temporary) != {name: ref.size for name, ref in prepared.refs.items()}:
                raise ContentConflict("Prepared submission files changed before staging")
            for name, body in added.items():
                target = _path(temporary, name)
                _write_new(target, body)
                _normal_path(target).chmod(stat.S_IREAD)
            _write_new(temporary / "manifest.json", manifest)
            _normal_path(temporary / "manifest.json").chmod(stat.S_IREAD)
            verify_references(temporary, [refs[name] for name in added])
            if digest(json.loads(_read(temporary / "record.json"))) != metadata["body_sha256"]:
                raise ContentError("Frozen artifact record differs from its identity")
            _directory(destination.parent)
            os.rename(_plain(temporary), _plain(destination))
        finally:
            if _normal_path(temporary).exists():
                self.content._discard_temporary(temporary)

    def verify(self, directory: Path, artifact_id: str, version: int, body_sha256: str) -> dict:
        """Verify all original and final files against the committed archive manifest."""
        _identity(artifact_id)
        _hash(body_sha256)
        body = _read(directory / "manifest.json")
        manifest = json.loads(body)
        if (manifest.get("schema_version") != 1 or manifest.get("artifact_id") != artifact_id
                or manifest.get("version") != version or manifest.get("body_sha256") != body_sha256
                or canonical(manifest).encode("utf-8") != body):
            raise ContentError("Artifact archive identity differs from its frozen reference")
        refs = [FileReference.from_dict(value) for value in manifest["files"]]
        expected = _paths([item.path for item in refs] + ["manifest.json"], max_files=_MAX_ARCHIVE_FILES)
        actual = []
        for current, dirs, names in os.walk(_plain(directory), followlinks=False):
            for name in dirs:
                _plain(Path(current) / name)
            for name in names:
                actual.append(_plain(Path(current) / name).relative_to(directory).as_posix())
        if _paths(actual, max_files=_MAX_ARCHIVE_FILES) != expected:
            raise ContentError("Frozen artifact files differ from their manifest")
        if sum(item.size for item in refs) > _MAX_ARCHIVE_BYTES:
            raise ContentError("Artifact archive exceeds its source-and-evidence bound")
        for item in refs:
            verify_references(directory, [item])
        if digest(json.loads(_read(directory / "record.json"))) != body_sha256:
            raise ContentError("Frozen artifact record differs from its identity")
        return manifest

    def resolve(self, path: Path, artifact_id: str, body_sha256: str) -> Path:
        """Follow only hash-bound relocation receipts; never fall back to blob bodies."""
        path = _plain(path)
        seen: set[Path] = set()
        while True:
            if path in seen or len(seen) >= 4:
                raise ContentError("Invalid artifact relocation chain")
            seen.add(path)
            journal_root = _path(self.journals, _identity(artifact_id))
            source = path.parent.relative_to(self.root).as_posix()
            matches = []
            if _normal_path(journal_root).is_dir():
                for item in _normal_path(journal_root).iterdir():
                    journal_body = _read(_normal_path(item))
                    receipt = json.loads(journal_body)
                    if receipt.get("source") == source:
                        matches.append((receipt, journal_body))
            if not matches:
                if not _normal_path(path).is_file():
                    raise ContentError("Frozen artifact record is missing without a relocation receipt")
                return path
            if len(matches) != 1:
                raise ContentError("Ambiguous artifact relocation receipts")
            receipt, journal_body = matches[0]
            marker = _path(path.parent, "relocated.json")
            version = receipt.get("version")
            journal = self._journal(artifact_id, version, path.parent)
            if (_read(journal) != journal_body
                    or (_normal_path(marker).exists() and _read(marker) != journal_body)):
                raise ContentError("Artifact relocation marker differs from its journal")
            if (receipt.get("artifact_id") != artifact_id or receipt.get("body_sha256") != body_sha256
                    or receipt.get("source") != source):
                raise ContentError("Artifact relocation does not bind the requested submission")
            path = _path(self.root, receipt["destination"] + "/record.json")

    def relocate(self, source: Path, artifact_id: str, data: dict, area: str,
                 receipt: dict, extra_files: Mapping[str, bytes] | None = None) -> Path:
        """Install and verify the destination, journal it, then replace the old directory.

        A copy is installed before removing the old location so even interruption
        during cleanup preserves one complete verified copy. Existing immutable
        destinations must match exactly; retries never overwrite them.

        The copy is read, written, flushed and verified outside the content lock, which is
        held only to rename it into place, journal the move and remove the old folder. In
        the Linux pilot an accepted archive (1,029 files) took 50 to 85 s under the lock.
        """
        version, body_sha = self.version(data), digest(data)
        source = _plain(source)
        destination = self.location(artifact_id, version, area)
        prepared: dict[str, Any] | None = None
        try:
            try:
                current = self.resolve(source / "record.json", artifact_id, body_sha).parent
                if current != destination:
                    prepared = self._moved_archive(current, artifact_id, version, body_sha, area, receipt, extra_files)
                    prepared["staged"] = self._stage_archive(destination, prepared["files"], prepared["metadata"])
            except (ContentError, OSError, ValueError):
                prepared = None  # Moved or changed meanwhile: the locked steps below decide.
            with self.content._locked():
                # Another thread may have finished this move while this one waited for the
                # lock; the old folder then names its new location in the journal.
                source = self.resolve(source / "record.json", artifact_id, body_sha).parent
                if source == destination:
                    manifest = self.verify(destination, artifact_id, version, body_sha)
                    if area != "staging":
                        receipt_name = "publication.json" if area == "artifacts" else "history.json"
                        if _read(destination / receipt_name) != canonical(receipt).encode("utf-8"):
                            raise ContentConflict("Recovered archive receipt differs from the coordinated evidence")
                    for name, body in (extra_files or {}).items():
                        if _read(_path(destination, name)) != body:
                            raise ContentConflict("Recovered integrated file differs from its publication snapshot")
                    if area == "artifacts" and manifest.get("disposition") != "published":
                        raise ContentError("An accepted archive must have a published disposition")
                    self._repair_markers(artifact_id, body_sha)
                    return destination / "record.json"
                if prepared is None or prepared["source"] != source:
                    if prepared is not None and prepared["staged"] is not None:
                        self.content._discard_temporary(prepared["staged"])
                    prepared = self._moved_archive(source, artifact_id, version, body_sha, area, receipt, extra_files)
                staged, prepared["staged"] = prepared["staged"], None
                self._install(destination, prepared["files"], prepared["metadata"], staged=staged)
                relocation = {"schema_version": 1, "artifact_id": artifact_id, "version": version,
                              "body_sha256": body_sha, "source": source.relative_to(self.root).as_posix(),
                              "destination": destination.relative_to(self.root).as_posix()}
                journal_body = canonical(relocation).encode("utf-8")
                journal = self._journal(artifact_id, version, source)
                if _normal_path(journal).exists():
                    if _read(journal) != journal_body:
                        raise ContentConflict("Artifact relocation journal already differs")
                else:
                    _write_new(journal, journal_body)
                # Remove exactly the verified files, not arbitrary recursively found
                # content. A crash leaves the complete destination and journal intact.
                for name in sorted(prepared["original_names"], reverse=True):
                    target = _path(source, name)
                    if _normal_path(target).exists():
                        _normal_path(target).chmod(stat.S_IREAD | stat.S_IWRITE)
                        _normal_path(target).unlink()
                manifest_path = source / "manifest.json"
                if _normal_path(manifest_path).exists():
                    _normal_path(manifest_path).chmod(stat.S_IREAD | stat.S_IWRITE)
                    _normal_path(manifest_path).unlink()
                _write_new(source / "relocated.json", journal_body)
                return destination / "record.json"
        finally:
            if prepared is not None and prepared["staged"] is not None and _normal_path(prepared["staged"]).exists():
                self.content._discard_temporary(prepared["staged"])

    def _moved_archive(self, source: Path, artifact_id: str, version: int, body_sha: str, area: str,
                       receipt: dict, extra_files: Mapping[str, bytes] | None) -> dict[str, Any]:
        """The verified files of the archive at `source` as they will be at their new location."""
        original = _read(source / "record.json")
        if digest(json.loads(original)) != body_sha:
            raise ContentError("Frozen submission changed before relocation")
        files = {"record.json": original}
        manifest = self.verify(source, artifact_id, version, body_sha)
        files.update({ref["path"]: _read(_path(source, ref["path"])) for ref in manifest["files"]
                      if ref["path"] != "record.json"})
        original_names = set(files)
        files.update(extra_files or {})
        if area != "staging":
            files["publication.json" if area == "artifacts" else "history.json"] = canonical(receipt).encode("utf-8")
        disposition = "published" if area == "artifacts" else receipt.get("disposition", "staged")
        return {"source": source, "files": files, "original_names": original_names, "staged": None,
                "metadata": {"artifact_id": artifact_id, "version": version, "body_sha256": body_sha,
                             "disposition": disposition}}

    def _repair_markers(self, artifact_id: str, body_sha: str) -> None:
        root = _path(self.journals, artifact_id)
        if not _normal_path(root).is_dir():
            return
        for journal in _normal_path(root).iterdir():
            body = _read(_normal_path(journal))
            receipt = json.loads(body)
            if receipt.get("artifact_id") != artifact_id or receipt.get("body_sha256") != body_sha:
                raise ContentError("Artifact recovery journal does not bind the record")
            target = self.resolve(_path(self.root, receipt["destination"] + "/record.json"), artifact_id, body_sha)
            self.verify(target.parent, artifact_id, receipt["version"], body_sha)
            marker = _path(self.root, receipt["source"] + "/relocated.json")
            if _normal_path(marker).exists():
                if _read(marker) != body:
                    raise ContentConflict("Artifact relocation marker was changed")
            else:
                _write_new(marker, body)
