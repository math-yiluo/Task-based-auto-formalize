"""Public notes require immutable source files and the ordinary merge boundary."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.runtime.review_entry import prepare_review_entry
from formal_geyser.coordination.runtime.tools import operation_tools
from formal_geyser.coordination.storage import digest
from tests.coordination.lifecycle_helpers import select_review
from tests.coordination.publication_helpers import file_payload, new_hub


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             actor or Actor(role=Role.OPERATOR))


def worker(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


async def assign(hub):
    task = await command(hub, "task.publish", {"contract": {
        "title": "Document a synthetic protocol", "objective": "Document the protocol in a file.",
        "acceptance_policy": "general.peer@1"}})
    run = await command(hub, "run.start")
    attempt = await hub.next_assignment(run["id"])
    return task, run, attempt


@pytest.mark.parametrize("name", ["artifact.stage", "artifact.submit"])
@pytest.mark.parametrize("selection", [None, [], ["notes/missing.md"]])
async def test_content_only_submission_is_rejected_before_draining(tmp_path, name, selection):
    hub = await new_hub(tmp_path)
    try:
        _, _, attempt = await assign(hub)
        payload = {"kind": "research.note", "data": {"summary": "An inline description is not source."}}
        if selection is not None:
            payload["repository_paths"] = selection
        with pytest.raises(HubError) as missing:
            await command(hub, name, payload, worker(attempt))
        assert missing.value.code == "SOURCE_FILES_REQUIRED"
        active = await hub.authorize_worker(worker(attempt))
        assert active["state"] == "running" and active["submission_json"] is None
        assert await hub.list_artifacts() == []
    finally:
        await hub.close()


async def test_selected_files_do_not_allow_publication_without_managed_repository(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        _, _, attempt = await assign(hub)
        payload = file_payload(hub, attempt, kind="note.wiki", data={"summary": "A source file"},
                               path="wiki/summary.json")
        with pytest.raises(HubError) as missing:
            await command(hub, "artifact.stage", payload, worker(attempt))
        assert missing.value.code == "SOURCE_FILES_REQUIRED"
        assert await hub.list_artifacts() == []
    finally:
        await hub.close()


@pytest.mark.parametrize("mutation", ["missing", "changed"])
async def test_review_reads_frozen_note_file_and_never_repairs_it_from_blob(tmp_path, mutation):
    hub = await new_hub(tmp_path)
    try:
        _, run, attempt = await assign(hub)
        payload = file_payload(hub, attempt, kind="research.note", data={"summary": "File-based evidence"},
                               path="notes/result.json")
        staged = await command(hub, "artifact.stage", payload, worker(attempt))
        artifact = await hub.artifact(staged["artifact_id"])
        reference = artifact["content_file"]["source_files"][0]
        frozen = Path(reference["path"])
        body = frozen.read_bytes()
        (hub.workspace(attempt["worker_id"]) / "notes/result.json").write_text("Later private draft", encoding="utf-8")
        reviewer = await select_review(hub, staged["review_task_id"], run["id"])
        entry = await prepare_review_entry(hub, reviewer)
        assert entry["candidate"]["source_files"] == [reference]
        assert frozen.read_bytes() == body
        assert not await hub.db.call(lambda c: hub._merge_published(c, artifact["id"]))
        if mutation == "missing":
            frozen.unlink()
        else:
            frozen.write_text("Unreviewed replacement", encoding="utf-8")
        # Exact original bytes remain available for validation; reads must still fail.
        assert hub.blobs.get(reference["sha256"]) == body
        with pytest.raises((HubError, ValueError, OSError)):
            await hub.artifact(artifact["id"])
        assert not frozen.exists() if mutation == "missing" else frozen.read_text() == "Unreviewed replacement"
    finally:
        await hub.close()


async def test_no_legacy_body_read_or_private_note_registration(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        _, _, attempt = await assign(hub)
        assert "taskhub_private" not in {item["name"] for item in operation_tools()}
        for name, actor in (("record.private", worker(attempt)),
                            ("artifact.register", Actor(role=Role.OPERATOR))):
            with pytest.raises(HubError) as missing:
                await command(hub, name, {"kind": "note.private", "data": {"text": "Write a file instead."}}, actor)
            assert missing.value.code == "NOTE_FILES_REQUIRED"
        # Machine/configuration records remain files, without a historical blob fallback.
        record = await command(hub, "artifact.register", {"kind": "configuration.fixture", "data": {"enabled": True}})
        assert json.loads(Path(record["content_file"]["path"]).read_text()) == {"enabled": True}
        assert hub.blobs.put_json(record["data"]) == record["body_sha"]
        await hub.db.call(lambda c: c.execute("DELETE FROM native_record_files WHERE record_id=?", (record["id"],)), write=True)
        with pytest.raises(HubError) as missing:
            await hub.artifact(record["id"])
        assert missing.value.code == "CONTENT_REFERENCE_MISSING"
    finally:
        await hub.close()


async def test_native_record_body_does_not_replace_a_reviewed_notes_source_binding(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        _, run, attempt = await assign(hub)
        payload = file_payload(hub, attempt, kind="research.note", data={"summary": "Selected source"})
        staged = await command(hub, "artifact.stage", payload, worker(attempt))
        reviewer = await select_review(hub, staged["review_task_id"], run["id"])
        record_file = Path(staged["content_file"]["path"])
        source_file = Path(staged["content_file"]["source_files"][0]["path"])
        configuration = await command(hub, "artifact.register", {
            "kind": "configuration.fixture", "data": {"enabled": True}})
        await hub.db.call(lambda c: c.execute(
            "DELETE FROM candidate_repositories WHERE artifact_id=?", (staged["artifact_id"],)),
            write=True)
        # Retain the old content-only candidate shape without inventing acceptance.
        assert json.loads(record_file.read_bytes()) == payload["data"] and source_file.is_file()
        assert await hub.db.call(lambda c: c.execute(
            "SELECT 1 FROM candidate_bindings WHERE artifact_id=?", (staged["artifact_id"],)).fetchone())
        assert not await hub.db.call(lambda c: c.execute(
            "SELECT 1 FROM repository_publications WHERE candidate_id=?", (staged["artifact_id"],)).fetchone())
        for read in (lambda: hub.artifact(staged["artifact_id"]),
                     lambda: hub.native_record(staged["artifact_id"], Actor(role=Role.OPERATOR)),
                     lambda: prepare_review_entry(hub, reviewer)):
            with pytest.raises(HubError) as missing:
                await read()
            assert missing.value.code == "CONTENT_REFERENCE_MISSING"
        assert (await hub.artifact(configuration["id"]))["data"] == {"enabled": True}
        assert record_file.is_file() and source_file.is_file()
    finally:
        await hub.close()


@pytest.mark.parametrize("kind,private", [("research.note", False), ("notes", True), ("wiki.entry", False)])
async def test_identifiable_legacy_note_bodies_cannot_read_as_operator_or_private_records(tmp_path, kind, private):
    hub = await new_hub(tmp_path)
    try:
        _, _, attempt = await assign(hub)
        record_id, data = uuid4().hex, {"text": "An old inline note body."}

        def historical(c):
            producer = attempt["worker_id"] if private else "operator"
            c.execute("INSERT INTO artifacts(id,kind,body_sha,producer,status,created_at) VALUES(?,?,?,?,?,?)",
                      (record_id, kind, digest(data), producer, "private" if private else "candidate", hub.clock()))
            hub._persist_record_content(c, hub._get(c, "artifacts", record_id), data, private=private)
            if private:
                c.execute("INSERT INTO private_records VALUES(?,?,?)", (record_id, producer, attempt["id"]))
            return c.execute("SELECT path FROM native_record_files WHERE record_id=?", (record_id,)).fetchone()[0]

        relative = await hub.db.call(historical, write=True)
        body = hub.root / relative
        assert json.loads(body.read_bytes()) == data
        actor = worker(attempt) if private else Actor(role=Role.OPERATOR)
        with pytest.raises(HubError) as missing:
            await hub.native_record(record_id, actor)
        assert missing.value.code == "CONTENT_REFERENCE_MISSING"
        diagnostic = await command(hub, "record.private", {
            "kind": "tool.diagnostic", "data": {"check": "pending"}}, worker(attempt))
        assert (await hub.native_record(diagnostic["id"], worker(attempt)))["data"] == {"check": "pending"}
        assert body.is_file()
    finally:
        await hub.close()


