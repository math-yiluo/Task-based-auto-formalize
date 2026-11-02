"""Synthetic privacy and native-authority checks for Git collaboration exports."""
from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from formal_geyser.coordination import sync_state
from formal_geyser.coordination.content import ContentError
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    RepositoryConfig,
    Role,
)
from formal_geyser.coordination.snapshot_limits import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
)
from formal_geyser.lean_service.executor import (
    snapshot_sources as lean_snapshot_sources,
)
from formal_geyser.registry import (
    RegistryError,
    read_snapshot,
    snapshot_sources,
)
from formal_geyser.submission_records import _paths as publication_paths

from .publication_helpers import stage_note
from .test_git_sync import case as case
from .test_git_sync import command, snapshot


async def test_export_indexes_distinguish_notes_candidates_library_and_wiki(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig(repository=RepositoryConfig(enabled=True)))
    try:
        source = {"Math/Item.lean": b"def statement : Prop := True\r\n",
                  "Math/Item.md": b"# Statement\n", "wiki/Start.md": b"# Read before exploring\n"}
        base = await command(hub, "repository.initialize", {"snapshot": snapshot(hub, source)})
        shared = await stage_note(hub, kind="research.observation",
                                  data={"observation": "Shared note", "native": True})
        # A real paired source supplies this pending classification fixture;
        # neither its kind nor a frozen snapshot claims mathematical acceptance.
        attempt = (await hub.task(shared["task_id"]))["active_attempts"][0]
        pending_sources = {"Math/Pending.lean": b"def pending : Nat := 1\n",
                           "Math/Pending.md": b"Synthetic pending definition; no acceptance claimed.\n"}
        workspace = hub.workspace(attempt["worker_id"])
        for relative, body in pending_sources.items():
            (workspace / relative).write_bytes(body)
        frozen = hub.blobs.snapshot(workspace)
        candidate = await hub.execute(Command(name="artifact.stage", key=uuid4().hex,
            payload={"kind": "lean.definition", "repository_paths": list(pending_sources), "data": {
                "description": "Synthetic pending mathematical metadata", "bundle": {
                    "snapshot": frozen, "lean_path": "Math/Pending.lean", "markdown_path": "Math/Pending.md",
                    "lean_sha256": hub.blobs.put(pending_sources["Math/Pending.lean"]),
                    "markdown_sha256": hub.blobs.put(pending_sources["Math/Pending.md"])}}}),
            Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                  fencing_token=attempt["fencing_token"]))
        assert not (await hub.artifact(candidate["id"]))["accepted"]
        files, _ = await sync_state.export_files(hub, main_commit=base["head"])
        assert [item["id"] for item in json.loads(files["notes/index.json"])] == [shared["id"]]
        assert [item["id"] for item in json.loads(files["artifacts/index.json"])] == [candidate["id"]]
        note = json.loads(files[f"notes/{shared['id']}.json"])
        assert note["category"] == "note" and note["data"] == shared["data"]
        assert "content_file" not in note
        assert f"artifacts/{shared['id']}.json" not in files
        assert not any(path.startswith("library/") for path in files)
        assert json.loads(files["knowledge/files.json"])[0]["path"] == "wiki/Start.md"
        assert json.loads(files["knowledge/current.json"])["commit"] == base["head"]
        assert not any(body in files.values() for body in source.values())
        assert str(tmp_path).encode() not in b"\n".join(files.values())
    finally:
        await hub.close()


async def test_private_diagnostics_and_talk_leave_no_shared_payload_or_record_paths(case, tmp_path):
    before, before_sequence = await sync_state.export_files(case.hub, main_commit=case.base)
    def create_private(c):
        note = case.hub._insert_artifact(c, "tool.diagnostic", {"secret": "private-body-marker"},
                                        task=case.task, producer="worker", private=True)
        path = note["content_file"]["path"]
        # Native ownership alone also protects a note if its old status is stale.
        case.hub._event(c, "record.private", note["id"], {"path": path, "text": "private-event-marker"},
                        task_id=case.task["id"])
        case.hub._event(c, "talk.message", uuid4().hex, {"text": "private-talk-marker"},
                        task_id=case.task["id"])
        return note, path
    private, path = await case.hub.db.call(create_private, write=True)
    contents, sequence = await sync_state.export_files(case.hub, main_commit=case.base)
    combined = b"\n".join(contents.values())
    for marker in ("private-body-marker", "private-event-marker", "private-talk-marker", path, private["id"]):
        assert marker.encode() not in combined
    assert contents == before and sequence == before_sequence
    await case.hub.close()
    backup = tmp_path / "backup"
    shutil.copytree(case.root, backup, copy_function=shutil.copyfile)
    copied = backup / Path(path).relative_to(case.root)
    assert copied.read_bytes() == Path(path).read_bytes()


async def test_task_revision_export_uses_native_authority_and_does_not_fall_back(case):
    await case.hub.db.call(lambda c: c.execute(
        "UPDATE task_revisions SET contract_json=? WHERE task_id=?", ("invalid cache JSON", case.task["id"])),
        write=True)
    files, _ = await sync_state.export_files(case.hub, main_commit=case.base)
    revision = next(item for item in json.loads(files["task-revisions.json"])
                    if item["task_id"] == case.task["id"])
    assert revision["contract"] == case.task["contract"]
    native = case.hub.content.task_files(case.task["id"], case.task["revision"], case.task["contract"])
    objective = native.root / "objective.md"
    objective.chmod(stat.S_IREAD | stat.S_IWRITE)
    objective.write_text("Tampered native objective", encoding="utf-8")
    with pytest.raises(ContentError, match="integrity"):
        await sync_state.export_files(case.hub, main_commit=case.base)


async def test_shared_note_export_uses_native_file_without_blob_fallback(case):
    note = await stage_note(case.hub, data={"observation": "Native shared content"})
    # A valid cached blob cannot repair a missing authoritative file.
    assert case.hub.blobs.put_json(note["data"]) == note["body_sha"]
    Path(note["content_file"]["path"]).unlink()
    with pytest.raises((HubError, FileNotFoundError, ContentError)):
        await sync_state.export_files(case.hub, main_commit=case.base)


@pytest.mark.parametrize("name", [".private", ".PrIvAtE", ".prıvate"])
async def test_private_directories_are_excluded_or_rejected_at_every_source_boundary(case, name):
    source = case.hub.workspace("source-author")
    private = source / "nested" / name / "notes"
    private.mkdir(parents=True)
    secret = private / "research.md"
    secret.write_bytes(b"private source content")
    (source / "public.md").write_bytes(b"source note")
    sha = snapshot_sources(case.hub, source)
    assert [item["path"] for item in read_snapshot(case.hub.blobs, sha)] == ["public.md"]
    assert secret.read_bytes() == b"private source content"
    with pytest.raises(RegistryError, match="Private files"):
        snapshot_sources(case.hub, private)
    path = f"nested/{name}/notes/research.md"
    malicious = snapshot(case.hub, {path: b"private source content"})
    with pytest.raises(RegistryError, match="Private notes"):
        read_snapshot(case.hub.blobs, malicious)
    # The Lean service reads frozen sources through the same boundary.
    with pytest.raises(RegistryError, match="Private notes"):
        lean_snapshot_sources(case.hub.blobs, malicious, max_files=MAX_SNAPSHOT_FILES, max_bytes=MAX_SNAPSHOT_BYTES)
    with pytest.raises(ValueError, match="Private notes"):
        publication_paths([path])
