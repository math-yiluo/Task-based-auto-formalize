from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Generator, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from copy import deepcopy
from itertools import islice
from pathlib import Path
from typing import Any, Literal, overload

from formal_geyser.coordination.content import PRIVATE_DIRECTORY
from formal_geyser.coordination.draft_paths import (
    is_draft_path,
    is_shared_draft_directory,
)
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.graph_paths import (
    is_graph_path,
    is_shared_graph_directory,
)
from formal_geyser.coordination.models import Actor, Command, Role, TaskContract
from formal_geyser.coordination.snapshot_limits import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
)
from formal_geyser.coordination.storage import digest, snapshot_files

from .paths import content_hash, relative_path, safe_child
from .schemas import (
    APPROVAL_POLICIES,
    OBJECT_ADAPTER,
    BuildCandidateRecord,
    DefinitionRecord,
    DomainObject,
    EnvironmentRecord,
    MathematicalRecord,
    ObjectRecord,
    ProofRecord,
    SourceLocation,
    StatementRecord,
    stored_record,
)


class RegistryError(ValueError):
    pass


class ApprovalRequired(RegistryError):
    pass


class EnvironmentUnavailable(RegistryError):
    """A missing or changed registered installation cannot judge a candidate."""


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while body := stream.read(1024 * 1024):
            hasher.update(body)
    return hasher.hexdigest()


def source_directory(hub, directory: Path) -> Path:
    """A directory that may be read as source: not shared drafts or graphs, not private notes,
    and not containing the core state."""
    directory = directory.resolve()
    if is_shared_draft_directory(directory, Path(hub.root)):
        raise RegistryError("Shared drafts cannot be selected as a source snapshot")
    if is_shared_graph_directory(directory, Path(hub.root)):
        raise RegistryError("Shared graphs cannot be selected as a source snapshot")
    if any(part.upper().casefold() == PRIVATE_DIRECTORY for part in directory.parts):
        raise RegistryError("Private files (.private) are never part of a source snapshot")
    if not directory.is_dir():
        raise RegistryError("Snapshot source must be an existing directory")
    if Path(hub.state_dir).resolve().is_relative_to(directory):
        raise RegistryError("A source snapshot cannot contain the core state directory; select a source subdirectory")
    return directory


def snapshot_sources(hub, directory: Path, *, max_files: int = 2048,
                     max_bytes: int = 67108864) -> str:
    """Capture the core snapshot format with bounds applied before file reads."""
    directory = source_directory(hub, directory)
    if max_files < 1 or max_bytes < 1:
        raise RegistryError("Snapshot limits must be positive")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    for path in snapshot_files(directory):
        relative = path.relative_to(directory).as_posix()
        source = safe_child(directory, relative)
        if len(entries) >= max_files:
            raise RegistryError("Snapshot file limit exceeded")
        if relative.casefold() in seen:
            raise RegistryError("Snapshot contains colliding paths")
        seen.add(relative.casefold())
        remaining = max_bytes - total
        if not source.is_file() or source.stat().st_size > remaining:
            raise RegistryError("Snapshot byte limit exceeded or source is not a regular file")
        with source.open("rb") as stream:
            body = stream.read(remaining + 1)
        if len(body) > remaining:
            raise RegistryError("Snapshot byte limit exceeded while reading a changing source")
        total += len(body)
        entries.append({"path": relative, "sha256": hub.blobs.put(body)})
    return hub.blobs.put_json({"files": sorted(entries, key=lambda entry: entry["path"])})


def _bounded_blob(blobs, sha: str, max_bytes: int) -> bytes:
    content_hash(sha)
    # The core content-addressed store is immutable; check its size before materializing bytes.
    stored = Path(blobs.root) / sha[:2] / sha
    if stored.stat().st_size > max_bytes:
        raise RegistryError("Snapshot byte limit exceeded before reading blob")
    body = blobs.get(sha)
    if len(body) > max_bytes:
        raise RegistryError("Snapshot blob changed beyond its byte limit")
    return body


# A stored blob's bytes are fixed by its SHA-256. Once read and verified, its size is reused
# while the stored file is present with that size; every use of the bytes still verifies them.
_VERIFIED_SIZES: dict[tuple[str, str], int] = {}
_VERIFIED_SIZES_LIMIT = 200_000


def _blob_size(blobs, sha: str, max_bytes: int) -> int:
    content_hash(sha)
    key = (str(blobs.root), sha)
    known = _VERIFIED_SIZES.get(key)
    if known is not None and (Path(blobs.root) / sha[:2] / sha).stat().st_size == known:
        if known > max_bytes:
            raise RegistryError("Snapshot byte limit exceeded before reading blob")
        return known
    size = len(_bounded_blob(blobs, sha, max_bytes))
    if len(_VERIFIED_SIZES) >= _VERIFIED_SIZES_LIMIT:
        _VERIFIED_SIZES.clear()
    _VERIFIED_SIZES[key] = size
    return size


def read_snapshot(blobs, snapshot: str, *, max_files: int = MAX_SNAPSHOT_FILES,
                  max_bytes: int = MAX_SNAPSHOT_BYTES) -> list[dict[str, str]]:
    content_hash(snapshot)
    manifest = json.loads(_bounded_blob(blobs, snapshot, max_files * 1200 + 1024))
    if not isinstance(manifest, dict) or set(manifest) != {"files"}:
        raise RegistryError("Invalid snapshot manifest")
    if not isinstance(manifest["files"], list) or len(manifest["files"]) > max_files:
        raise RegistryError("Snapshot file limit exceeded")
    seen: set[str] = set()
    total = 0
    result = []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise RegistryError("Invalid snapshot entry")
        relative_path(item["path"])
        if is_draft_path(item["path"]):
            raise RegistryError("Shared drafts cannot enter a source snapshot")
        if is_graph_path(item["path"]):
            raise RegistryError("Shared graphs cannot enter a source snapshot")
        if any(part.upper().casefold() == PRIVATE_DIRECTORY for part in item["path"].split("/")):
            raise RegistryError("Private notes cannot enter a source snapshot")
        content_hash(item["sha256"])
        key = item["path"].casefold()
        if key in seen:
            raise RegistryError("Snapshot contains colliding paths")
        seen.add(key)
        total += _blob_size(blobs, item["sha256"], max_bytes - total)
        if total > max_bytes:
            raise RegistryError("Snapshot byte limit exceeded")
        result.append(item)
    return result



def has_approval(artifact: dict, policy: str) -> bool:
    if artifact.get("status") == "withdrawn" or not artifact.get("accepted"):
        return False
    for item in reversed(artifact.get("evidence", [])):
        if item.get("policy") == policy:
            return item.get("verdict") == "accepted" and not item.get("qualifications", [])
    return False


def reference_ids(record: ObjectRecord) -> list[str]:
    """The record's environment is its only typed artifact reference."""
    value = getattr(record, "environment_artifact", None)
    return [value] if value else []


# Typed exact references carried by current records.
TYPED_REFERENCES = {"environment_artifact": "environment"}

# One variable per module, not per check. Values remain local to an async
# resolution; copied contexts must never turn this into a global approval cache.
_READ_SCOPES: ContextVar[dict[object, dict[str, dict[str, Any]]]] = ContextVar("registry_reads", default={})


class ObjectRegistry:
    """Application schemas stored exclusively through immutable core artifact APIs."""

    def __init__(self, hub):
        self.hub = hub

    def _read_cache(self):
        cache = _READ_SCOPES.get().get(self)
        return None if cache is not None and cache.get("_state", {}).get("closed") else cache

    @staticmethod
    def _artifact_identity(artifact: dict | None):
        if artifact is None:
            return None
        data = artifact.get("data", {})
        object_type = data.get("object_type")
        policy = APPROVAL_POLICIES.get(object_type) if isinstance(object_type, str) else None
        return digest({"id": artifact.get("id"), "kind": artifact.get("kind"),
                       "body_sha": artifact.get("body_sha"), "data": data,
                       "input_refs": artifact.get("input_refs", []),
                       "status": artifact.get("status"), "accepted": artifact.get("accepted"),
                       "approved": has_approval(artifact, policy) if policy else False})

    async def _read_once(self, kind, key):
        cache = self._read_cache()
        if cache is None:
            return await getattr(self.hub, kind)(key)
        values = cache.setdefault(kind, {})
        locks = cache.setdefault("_locks", {})
        async with locks.setdefault(kind + ":" + key, asyncio.Lock()):
            if key not in values:
                values[key] = deepcopy(await getattr(self.hub, kind)(key))
            return deepcopy(values[key])

    @asynccontextmanager
    async def verified_reads(self):
        """Reuse metadata within one resolution, then recheck each real input.

        Every first and final read uses the owner's existing serialized API.
        No independent SQLite reader, process-wide approval cache, or skipped
        file verification is introduced. Evidence commits still check current
        bindings after compilation.
        """
        if self._read_cache() is not None:
            yield
            return
        cache: dict[str, dict[str, Any]] = {}
        token = _READ_SCOPES.set({**_READ_SCOPES.get(), self: cache})
        try:
            yield
            for artifact_id, original in cache.get("artifact", {}).items():
                current = await self.hub.artifact(artifact_id)
                if self._artifact_identity(current) != self._artifact_identity(original):
                    raise EnvironmentUnavailable("Registry input changed during metadata resolution: " + artifact_id)
            for source_id, original in cache.get("accessible_literature", {}).items():
                current = await self.hub.accessible_literature(source_id)
                if current.get("source_sha256") != original.get("source_sha256"):
                    raise EnvironmentUnavailable("Literature changed during metadata resolution: " + source_id)
        except HubError as error:
            raise EnvironmentUnavailable("Registry input could not be rechecked: " + str(error)) from error
        finally:
            cache["_state"] = {"closed": True}
            _READ_SCOPES.reset(token)

    async def inspect(self, artifact_id: str) -> dict:
        """Inspect historical records without upgrading them to a current schema."""
        try:
            artifact = await self._read_once("artifact", artifact_id)
        except HubError as error:
            raise RegistryError(str(error)) from error
        if not artifact:
            raise RegistryError(f"Unknown artifact: {artifact_id}")
        return artifact

    @overload
    async def get(self, artifact_id: str, expected_type: Literal["definition"],
                  *, approved: bool = False) -> tuple[DefinitionRecord, dict]: ...

    @overload
    async def get(self, artifact_id: str, expected_type: Literal["statement"],
                  *, approved: bool = False) -> tuple[StatementRecord, dict]: ...

    @overload
    async def get(self, artifact_id: str, expected_type: Literal["proof"],
                  *, approved: bool = False) -> tuple[ProofRecord, dict]: ...

    @overload
    async def get(self, artifact_id: str, expected_type: Literal["build-candidate"],
                  *, approved: bool = False) -> tuple[BuildCandidateRecord, dict]: ...

    @overload
    async def get(self, artifact_id: str, expected_type: Literal["environment"],
                  *, approved: bool = False) -> tuple[EnvironmentRecord, dict]: ...

    @overload
    async def get(self, artifact_id: str, expected_type: str | None = None,
                  *, approved: bool = False) -> tuple[DomainObject, dict]: ...

    async def get(self, artifact_id: str, expected_type: str | None = None,
                  *, approved: bool = False) -> tuple[DomainObject, dict]:
        try:
            artifact = await self._read_once("artifact", artifact_id)
        except HubError as error:
            raise RegistryError(str(error)) from error
        if artifact is None:
            raise RegistryError(f"Unknown artifact: {artifact_id}")
        record = stored_record(artifact["data"])
        missing = set(reference_ids(record)) - set(artifact.get("input_refs", []))
        if missing:
            raise RegistryError("Domain record omits dependency references from core input_refs: " + ", ".join(sorted(missing)))
        if artifact["kind"] != f"lean.{record.object_type}":
            raise RegistryError("Artifact kind and object schema disagree")
        if expected_type is not None and record.object_type != expected_type:
            raise RegistryError(f"Expected {expected_type}, received {record.object_type}")
        if approved:
            if isinstance(record, MathematicalRecord):
                await self._validate_references(record)
            policy = APPROVAL_POLICIES.get(record.object_type)
            if policy is None or not has_approval(artifact, policy):
                raise ApprovalRequired(f"Artifact {artifact_id} requires an accepted {policy} receipt")
        return record, artifact

    async def stage(self, value: dict | ObjectRecord, actor: Actor, *, key: str | None = None) -> dict:
        record = OBJECT_ADAPTER.validate_python(
            value.model_dump(mode="json") if isinstance(value, ObjectRecord) else value
        )
        if actor.role == Role.OPERATOR and not isinstance(record, EnvironmentRecord):
            raise RegistryError("Operator registration is limited to environment configuration; author native files through an active task")
        await self._validate_references(record)
        body = record.model_dump(mode="json")
        name = "artifact.register"
        payload: dict[str, Any] = {"kind": f"lean.{record.object_type}", "data": body,
                                   "input_refs": reference_ids(record)}
        if actor.role == Role.WORKER:
            attempt = await self.hub.authorize_worker(actor)
            task = await self.hub.task(attempt["task_id"])
            if task.get("collaboration", {}).get("mode") != "collaborative":
                raise RegistryError("New records require native file staging in a current task")
            from .publication import publication_policy
            name = "artifact.stage"
            payload["policy"] = publication_policy(TaskContract.model_validate(task["contract"]), record)
        result = await self.hub.execute(Command(
            name=name, key=key or f"lean-register:{actor.attempt_id or 'operator'}:{digest(body)}",
            payload=payload,
        ), actor)
        if name == "artifact.stage":
            return {**result["artifact"], "review_task_id": result["review_task_id"]}
        return result

    async def _validate_references(self, record: DomainObject) -> None:
        if record.supersedes:
            old, _ = await self.get(record.supersedes, record.object_type)
            if record.name != old.name or record.version != old.version + 1:
                raise RegistryError("A successor must keep its name and advance exactly one version")
        for source in getattr(record, "sources", []):
            await self.source_evidence(source)
        for field, expected in TYPED_REFERENCES.items():
            ref = getattr(record, field, None)
            if ref:
                referred, _ = await self.get(ref, expected)
                version = getattr(record, field.removesuffix("_artifact") + "_version", None)
                if version is not None and referred.version != version:
                    raise RegistryError(f"Exact {field} version does not match its immutable artifact")
        if isinstance(record, MathematicalRecord):
            from .mathematical_artifacts import validate_math_record_snapshot
            environment, _ = await self.get(record.environment_artifact, "environment")
            await asyncio.to_thread(validate_math_record_snapshot, self.hub.blobs, record,
                                    max_files=environment.limits.snapshot_file_limit,
                                    max_bytes=environment.limits.snapshot_bytes)
        if isinstance(record, EnvironmentRecord):
            from .tooling import validate_library_isolated
            for library in record.library_roots:
                await validate_library_isolated(self.hub.blobs, library.path, library.manifest_sha256)

    async def validate_for_publication(self, record: DomainObject) -> None:
        """Check immutable metadata and exact approvals before peer-reviewed publication."""
        async with self.verified_reads():
            await self._validate_for_publication(record)

    async def _validate_for_publication(self, record: DomainObject) -> None:
        await self._validate_references(record)
        for source in getattr(record, "sources", []):
            await self.source_evidence(source)
        for field, expected in TYPED_REFERENCES.items():
            if reference := getattr(record, field, None):
                await self.get(reference, expected, approved=True)
        for reference in reference_ids(record):
            artifact = await self._read_once("artifact", reference)
            if not artifact or artifact.get("status") == "withdrawn" or not artifact.get("accepted"):
                raise ApprovalRequired("Publication requires a live approved reference: " + reference)

    async def source_evidence(self, source: SourceLocation) -> dict:
        """Resolve a native citation directly; acquisition never fabricates peer approval."""
        try:
            entry = await self._read_once("accessible_literature", source.literature_id)
        except (HubError, OSError) as error:
            raise EnvironmentUnavailable("The exact cited literature is unavailable: " + str(error)) from error
        if entry.get("source_sha256") != source.content_sha256:
            raise RegistryError("Native literature citation differs from the registered source fingerprint")
        return {"literature_id": source.literature_id, "content_sha256": source.content_sha256,
                "locator": source.locator, "source_path": entry["source_path"],
                "entry_path": entry["path"], "index_path": entry.get("index_path"),
                "source_kind": entry.get("source_kind", "file"),
                "title": entry["title"], "edition": entry.get("version"), "uri": entry.get("uri"),
                "citation": entry.get("citation", ""), "scope": "source-acquisition-only"}

    async def list(self, object_type: str | None = None) -> list[dict]:
        rows = await self.hub.list_artifacts(kind=f"lean.{object_type}" if object_type else None)
        return [row for row in rows if row.get("kind", "").startswith("lean.")]

    async def import_source(self, path: Path, actor: Actor, *, name: str | None = None,
                            version: int = 1, origin: str | None = None, max_bytes: int = 67108864,
                            key: str | None = None, source_id: str | None = None,
                            copy: bool = True) -> dict:
        """Register readable native literature; never author a SourceRecord/blob chain."""
        path = path.resolve()
        if type(version) is not int or version < 1:
            raise RegistryError("A source edition version must be a positive integer")
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise RegistryError("Source is not a regular file or exceeds the source size limit")
        with path.open("rb") as stream:
            body = stream.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise RegistryError("Source exceeded its size limit while being read")
        if path.suffix.lower() not in {".txt", ".md", ".tex", ".pdf"}:
            raise RegistryError("Supported source formats are UTF-8 TXT, Markdown, TeX, and PDF")
        title = name or path.name
        identity = source_id or "source-" + digest({"title": title, "sha256": hashlib.sha256(body).hexdigest(), "version": version})[:32]
        payload = {"source_id": identity, "title": title, "path": str(path), "copy": copy,
                   "version": str(version)}
        if origin is not None:
            payload["uri"] = origin
        return await self.hub.execute(Command(name="literature.register", key=key or "literature-import:" + digest(payload),
                                              payload=payload), actor)


LIBRARY_HASH_WORKERS = 8
LIBRARY_HASH_BATCH = 64

# Digests of the file identities (size, mtime, file ID) of libraries that passed
# a full byte validation in this process, keyed by root and manifest. Only routine
# development checks may reuse them; acceptance paths always re-hash.
_VERIFIED_LIBRARIES: dict[tuple[str, str], str] = {}


def library_key(location: str, manifest_sha256: str) -> tuple[str, str]:
    return str(Path(location).resolve()), manifest_sha256


def _library_entries(root: Path) -> Iterator[tuple[str, os.DirEntry]]:
    """Enumerate every regular file without following links or skipping unreadable subtrees.

    Entry types come from the directory listing itself, so a library of 100,000+
    files is walked without extra per-file system calls or path objects.
    """
    pending = [(os.fspath(root), "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise RegistryError("Library contains a linked path")
                relative = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    pending.append((entry.path, relative + "/"))
                elif entry.is_file(follow_symlinks=False):
                    yield relative_path(relative), entry
                else:
                    raise RegistryError("Library entries must be regular files")


def _library_files(root: Path) -> Iterator[str]:
    return (relative for relative, _ in _library_entries(root))


def _library_identities(root: Path) -> dict[str, tuple[int, int, int]]:
    result = {}
    for relative, entry in _library_entries(root):
        info = entry.stat(follow_symlinks=False)
        result[relative.casefold()] = (info.st_size, info.st_mtime_ns, entry.inode())
    return result


def _hash_library_file(root: str, relative: str) -> tuple[str, str]:
    # The relative path is validated and every caller walks the whole root for
    # links (before or after hashing), so a plain join stays inside the root.
    target = Path(os.path.join(root, *relative.split("/")))
    try:
        return relative, file_sha256(target)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as error:
        raise RegistryError("Registered library file is missing or not regular") from error


def _library_hashes(root: Path, relatives: Iterable[str]) -> Generator[tuple[str, str], None, None]:
    """Read every byte with a bounded pool and at most one small batch of queued futures."""
    iterator = iter(relatives)
    location = os.fspath(root)
    executor = ThreadPoolExecutor(max_workers=LIBRARY_HASH_WORKERS, thread_name_prefix="lean-library-hash")
    try:
        while batch := list(islice(iterator, LIBRARY_HASH_BATCH)):
            pending = [executor.submit(_hash_library_file, location, relative) for relative in batch]
            for future in as_completed(pending):
                yield future.result()
    finally:
        # Also cancel queued work when a consumer detects a mismatch or a reader raises.
        executor.shutdown(wait=True, cancel_futures=True)


def _identities_digest(identities: dict[str, tuple[int, int, int]]) -> str:
    return hashlib.sha256(json.dumps(sorted(identities.items()), separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_library(blobs, location: str, manifest_sha256: str, *,
                     reuse_unchanged: bool = False) -> Path:
    """Check a registered library's exact file set and content in this process.

    By default every byte is re-hashed. With ``reuse_unchanged`` (every check made
    through tooling.LeanTooling), a library whose files all keep the size, mtime
    and file ID seen at an earlier full validation in this process is accepted
    without re-reading it; any difference falls back to the full check. Registering
    an environment always re-hashes every byte.
    The coordination service uses tooling.validate_library_isolated instead.
    """
    key = library_key(location, manifest_sha256)
    root, seen, _ = check_library(blobs, location, manifest_sha256,
                                  reuse_digest=_VERIFIED_LIBRARIES.get(key) if reuse_unchanged else None)
    _VERIFIED_LIBRARIES[key] = seen
    return root


def check_library(blobs, location: str, manifest_sha256: str, *,
                  reuse_digest: str | None = None) -> tuple[Path, str, bool]:
    """Return the resolved root, its file-identity digest, and whether every byte was read.

    Reading is skipped only when ``reuse_digest`` (from an earlier full check) still
    matches the walked identities; any difference falls back to the full check.
    """
    root = Path(location)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise RegistryError("Library roots must be absolute, existing, unlinked directories")
    root = root.resolve()
    manifest = blobs.get_json(manifest_sha256)
    if not isinstance(manifest, dict) or set(manifest) != {"files"} or not isinstance(manifest["files"], list):
        raise RegistryError("Invalid library manifest")
    expected: dict[str, str] = {}
    relatives = []
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise RegistryError("Invalid library manifest entry")
        relative_path(item["path"])
        content_hash(item["sha256"])
        if item["path"].casefold() in expected:
            raise RegistryError("Library manifest contains colliding paths")
        expected[item["path"].casefold()] = item["sha256"]
        relatives.append(item["path"])
    # Walk first: reject links and an unexpected file set before reading any bytes.
    identities = _library_identities(root)
    seen = _identities_digest(identities)
    if reuse_digest is not None and reuse_digest == seen:
        return root, seen, False
    if set(expected) != set(identities):
        raise RegistryError("Registered library file set has changed")
    with closing(_library_hashes(root, relatives)) as hashes:
        for relative, actual_hash in hashes:
            if actual_hash != expected[relative.casefold()]:
                raise RegistryError("Registered library file has changed")
    return root, seen, True


def library_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RegistryError("Library roots must be unlinked directories")
    root = path.resolve()
    if not root.is_dir():
        raise RegistryError("Library root must be an existing directory")
    seen: set[str] = set()

    def relatives():
        for relative in _library_files(root):
            key = relative.casefold()
            if key in seen:
                raise RegistryError("Library contains colliding paths")
            seen.add(key)
            yield relative

    with closing(_library_hashes(root, relatives())) as hashes:
        files = [{"path": relative, "sha256": sha} for relative, sha in hashes]
    # Preserve the former pathlib ordering and therefore the canonical manifest digest.
    files.sort(key=lambda item: Path(item["path"]))
    return {"files": files}


def record_json(value: str | bytes) -> ObjectRecord:
    return OBJECT_ADAPTER.validate_python(json.loads(value))
