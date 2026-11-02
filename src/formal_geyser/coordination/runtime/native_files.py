"""Read supplied native file paths through the existing bounded file tools.

Research content is readable throughout this project, including notes and talk.
Reading grants no mutation authority.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..artifact_files import BASE_FILE, ArtifactFiles
from ..content import ContentError, _normal_path, _plain, _read
from ..errors import HubError
from ..native import MATHEMATICAL_KINDS
from .files import ResourceRoots, ScopedFiles

_PUBLIC_FOLDERS = ("tasks", "wiki", "literature", "library", "drafts", "graphs")
_RECORD_FOLDERS = {"staging": "staging", "artifacts": "artifacts",
                   "submission_history": "submission-history", "notes": "notes"}


def native_resource_paths(hub: Any) -> dict[str, str]:
    return {"project": str(hub.root), **{name: str(hub.root / folder) for name, folder in
            [*((name, name) for name in _PUBLIC_FOLDERS), *_RECORD_FOLDERS.items()]}}


def _absolute(path: str) -> Path:
    if not isinstance(path, str) or "\x00" in path or ".." in Path(path).parts:
        raise ValueError("Native reads require a supplied file path without parent traversal")
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("Native reads require an absolute file path")
    # Reject links before normalization can obscure the path being authorized.
    return _plain(target)


class _ExactFile(ScopedFiles):
    def __init__(self, target: Path, sha256: str):
        super().__init__(target.parent)
        self.target, self.sha256 = target, sha256

    def path(self, relative: str, *, must_exist: bool = True) -> Path:
        target = super().path(relative, must_exist=must_exist)
        if target != self.target:
            raise HubError("FORBIDDEN", "Only the requested native file is authorized", 403)
        return target

    def _stream_file(self, target, consume, expected_sha256=None):
        return super()._stream_file(target, consume, expected_sha256 or self.sha256)


def _record_file(hub: Any, c: Any, actor: Any, target: Path, area: Path) -> tuple[ScopedFiles, str]:
    parts = target.relative_to(area).parts
    if len(parts) < 2:
        raise ValueError("Use an exact candidate record or source file path from review or fetch")
    record = c.execute("SELECT * FROM artifacts WHERE id=?", (parts[0],)).fetchone()
    if record is None or hub._record_private(c, record):
        raise HubError("FORBIDDEN", "This native record is not visible to the current worker", 403)
    record = dict(record)
    data = hub._record_body(c, record)
    reference = hub._record_files(c, record)
    root = _normal_path(Path(reference["root"]))
    # Old packet paths may survive a journaled relocation to an accepted archive.
    native = c.execute("SELECT path FROM native_record_files WHERE record_id=?", (record["id"],)).fetchone()
    original = _normal_path(hub.root / native["path"]).parent
    if target.is_relative_to(root):
        relative = target.relative_to(root).as_posix()
    elif target.is_relative_to(original):
        relative = target.relative_to(original).as_posix()
    elif record["kind"] in MATHEMATICAL_KINDS:
        store = ArtifactFiles(hub.content)
        relative = ""
        for previous in (area / record["id"] / str(store.version(data)), area / record["id"]):
            if not target.is_relative_to(previous):
                continue
            try:
                resolved = store.resolve(previous / "record.json", record["id"], record["body_sha"])
            except ContentError:
                continue
            if resolved.parent == root:
                relative = target.relative_to(previous).as_posix()
                break
    else:
        raise HubError("FORBIDDEN", "The path does not belong to the recorded native version", 403)
    allowed = {"record.json": reference["sha256"]}
    if record["kind"] in MATHEMATICAL_KINDS:
        manifest = root / "manifest.json"
        if manifest.is_file():
            # _record_body checked the pinned manifest; the one file read is checked against it.
            allowed.update({item["path"]: item["sha256"] for item in json.loads(_read(manifest))["files"]})
        else:
            bundle = data.get("bundle", {})
            allowed.update({"files/" + bundle[name + "_path"]: bundle[name + "_sha256"]
                            for name in ("lean", "markdown") if name + "_path" in bundle})
    else:
        allowed.update({Path(item["path"]).relative_to(root).as_posix(): item["sha256"]
                        for item in reference.get("source_files", [])})
    if relative not in allowed and relative.startswith("files/") and BASE_FILE in allowed:
        unchanged = _base_file(hub, root, allowed[BASE_FILE], relative.removeprefix("files/"))
        if unchanged is not None:
            return unchanged
    if relative not in allowed:
        raise HubError("FORBIDDEN", "The path is not a frozen file of this native record", 403)
    resolved = root / relative
    return _ExactFile(resolved, allowed[relative]), resolved.name


def _base_file(hub: Any, root: Path, base_sha256: str, path: str) -> tuple[ScopedFiles, str] | None:
    """A file of the candidate's snapshot that its archive leaves to its Library base: read from
    the base commit's published view, with the snapshot's hash; None when the snapshot has none."""
    body = _read(root / BASE_FILE)
    if hashlib.sha256(body).hexdigest() != base_sha256:
        raise HubError("CONTENT_CHANGED", "The archive's Library base differs from its manifest")
    base = json.loads(body)
    sha256 = {item["path"]: item["sha256"] for item in hub.blobs.get_json(base["snapshot"])["files"]}.get(path)
    if sha256 is None:
        return None
    view = _normal_path(hub.content.library_root / base["base_commit"] / "files" / path)
    if not view.is_file():
        raise HubError("FORBIDDEN", f"This file is the Library's at commit {base['base_commit']}, unchanged; "
                                    "read it in the Library", 403)
    return _ExactFile(view, sha256), view.name


async def native_file(hub: Any, actor: Any, resources: ResourceRoots,
                      path: str) -> tuple[ScopedFiles, str]:
    """Resolve an exact absolute path, or a path in the caller's own workspace."""
    if not isinstance(path, str):
        raise ValueError("Path must be a string")
    absolute = Path(path).is_absolute()
    target = _absolute(path) if absolute else None

    def resolve(c):
        hub._record_reader(c, actor)
        workspace = ScopedFiles(hub.workspace(actor.worker_id))
        if target is None:
            return workspace, path
        if target.is_relative_to(workspace.root):
            return workspace, target.relative_to(workspace.root).as_posix()
        if target.is_relative_to(hub.root):
            project_relative = target.relative_to(hub.root).as_posix()
            if not target.exists():
                for folder in _RECORD_FOLDERS.values():
                    area = _normal_path(hub.root / folder)
                    if target.is_relative_to(area):
                        return _record_file(hub, c, actor, target, area)
            return ScopedFiles(hub.root), project_relative
        for resource in resources.roots.values():
            if target.is_relative_to(resource.root):
                relative = target.relative_to(resource.root)
                return resource, relative.as_posix() if relative.parts else ""
        raise HubError("FORBIDDEN", "The path is outside the worker's readable native files", 403)

    return await hub.db.call(resolve)


async def native_resource(hub: Any, actor: Any, resources: ResourceRoots,
                          name: str, path: str = "") -> tuple[ScopedFiles, str]:
    """Resolve project or declared read-only resource paths, including aliases."""
    native = native_resource_paths(hub)
    aliases = {"central-library": "central_literature", "central_library": "central_literature",
               "lib": "library", "literature_library": "literature",
               "submission-history": "submission_history"}
    if not Path(name).is_absolute():
        known = {key.casefold(): key for key in {*native, *resources.roots}}
        name = aliases.get(name.casefold(), known.get(name.casefold(), name))
    if Path(path).is_absolute():
        return await native_file(hub, actor, resources, path)
    if Path(name).is_absolute():
        root = _absolute(name)
    elif name in native:
        root = Path(native[name])
    elif name in resources.roots:
        root = resources.get(name).root
    else:
        raise ValueError("Unknown resource root. Available roots: " + ", ".join(sorted(set(native) | resources.roots.keys())))
    if ".." in Path(path).parts or Path(path).drive or Path(path).root or ":" in path:
        raise ValueError("Path must stay within its declared root")
    return await native_file(hub, actor, resources, str(root / path))
