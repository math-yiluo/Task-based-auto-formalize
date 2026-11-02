"""Stable mathematical identities across storage, publication, and withdrawal.

These are synthetic file and metadata fixtures; no model or Lean process runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from formal_geyser.coordination import sync_state
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import ProjectConfig, RepositoryConfig

from .publication_helpers import approve, file_payload
from .test_archive_publication import archive_case as archive_case
from .test_archive_publication import command, manifest, quiet, remove_current_pair, worker
from .test_review_lifecycle import decision


@pytest.fixture
async def classification(tmp_path):
    initialize(tmp_path, ProjectConfig(repository=RepositoryConfig(enabled=True)))
    hub = Hub(tmp_path)
    try:
        initial = await command(hub, "repository.initialize", {"snapshot": manifest(hub, {})})
        task = await command(hub, "task.publish", {"contract": {
            "title": "Artifact classification fixture", "objective": "Inspect stored metadata only.",
            "acceptance_policy": "general.peer@1"}})
        owner = await command(hub, "worker.register")
        run = await command(hub, "run.start")
        attempt = await command(hub, "task.claim", {
            "task_id": task["id"], "revision": task["revision"],
            "worker_id": owner["id"], "run_id": run["id"],
        })
        staged = await command(hub, "artifact.stage", file_payload(
            hub, attempt, kind="research.note", data={"text": "Shared note"},
            path="notes/classification.json"), worker(attempt))
        note = await hub.artifact(staged["artifact_id"])
        assert note["status"] == "staged" and not note["accepted"]
        assert json.loads(Path(note["content_file"]["source_files"][0]["path"]).read_bytes()) == note["data"]

        def populate(c):
            shared = []
            for kind in ("lean.definition", "lean.statement", "lean.proof"):
                for status in ("candidate", "staged", "rejected", "accepted", "withdrawn"):
                    lean = b"-- Paired storage fixture only.\n"
                    markdown = b"Paired storage fixture; no theorem is asserted.\n"
                    snapshot = manifest(hub, {"Fixture.lean": lean, "Fixture.md": markdown})
                    data = {"version": 1, "name": f"{kind} {status} metadata fixture", "bundle": {
                        "lean_path": "Fixture.lean", "markdown_path": "Fixture.md",
                        "lean_sha256": hub.blobs.put(lean), "markdown_sha256": hub.blobs.put(markdown)}}
                    record = hub._insert_artifact(c, kind, data, snapshot=snapshot, task=task)
                    # This tests classification of stored states, not acceptance
                    # authority. No publication or current membership is invented.
                    c.execute("UPDATE artifacts SET status=? WHERE id=?", (status, record["id"]))
                    shared.append(hub._artifact(c, record["id"]))
            private = []
            for kind in ("lean.definition", "tool.diagnostic"):
                item = hub._insert_artifact(c, kind, {"secret": "private-classification-marker"},
                                            task=task, producer=owner["id"], private=True)
                # Ownership must remain authoritative even with a stale status.
                c.execute("UPDATE artifacts SET status='accepted' WHERE id=?", (item["id"],))
                private.append(item)
            status_only = hub._insert_artifact(c, "lean.statement", {"secret": "private-status-marker"},
                                              task=task)
            c.execute("UPDATE artifacts SET status='private' WHERE id=?", (status_only["id"],))
            private.append(status_only)
            return shared, private

        shared, private = await hub.db.call(populate, write=True)
        assert {record["category"] for record in private} == {"artifact", "note"}
        yield SimpleNamespace(hub=hub, root=tmp_path, task=task, initial=initial,
                              shared=shared, note=note, private=private)
    finally:
        await hub.close()


async def test_shared_artifact_list_covers_all_kinds_and_states_with_exact_identity(classification):
    case = classification
    listed = {record["id"]: record for record in await case.hub.list_math_artifacts()}
    assert set(listed) == {record["id"] for record in case.shared}
    assert {record["status"] for record in listed.values()} == {
        "candidate", "staged", "rejected", "accepted", "withdrawn"}
    for original in case.shared:
        retained = listed[original["id"]]
        assert retained["category"] == "artifact"
        assert retained["content_file"]["category"] == "artifact"
        for field in ("kind", "status", "body_sha", "snapshot", "data", "evidence", "content_file"):
            assert retained[field] == original[field]
        assert "library_membership" not in retained
    assert [record["id"] for record in await case.hub.list_notes()] == [case.note["id"]]


async def test_shared_lists_skip_private_owned_bodies_before_reading(classification):
    case = classification
    for record in case.private:
        body = Path(record["content_file"]["path"])
        body.chmod(0o600)
        body.write_bytes(b"Unreadable private fixture; shared listing must never open it.")
    assert {record["id"] for record in await case.hub.list_math_artifacts()} == {
        record["id"] for record in case.shared}
    assert [record["id"] for record in await case.hub.list_notes()] == [case.note["id"]]
    assert await case.hub.list_notes(kind="lean.definition") == []
    files, _ = await sync_state.export_files(case.hub, main_commit=case.initial["head"])
    combined = b"\n".join(files.values())
    for record in case.private:
        assert record["id"].encode() not in combined
    assert b"private-classification-marker" not in combined
    assert b"private-status-marker" not in combined


async def test_sync_exports_all_artifacts_without_library_records(classification):
    case = classification
    files, _ = await sync_state.export_files(case.hub, main_commit=case.initial["head"])
    indexed = {item["id"]: item for item in json.loads(files["artifacts/index.json"])}
    assert set(indexed) == {record["id"] for record in case.shared}
    for record in case.shared:
        item = indexed[record["id"]]
        assert item["category"] == "artifact"
        assert item["status"] == record["status"]
        assert item["body_sha"] == record["body_sha"]
        assert item["snapshot"] == record["snapshot"]
        assert item["path"] == f"artifacts/{record['id']}.json"
        exported = json.loads(files[item["path"]])
        for field in ("id", "category", "kind", "status", "body_sha", "snapshot", "data", "evidence"):
            assert exported[field] == record[field]
        assert "content_file" not in exported
        assert "library_membership" not in exported
    task = json.loads(files[f"tasks/{case.task['id']}/task.json"])
    assert {item["id"] for item in task["artifacts"]} == set(indexed)
    assert [item["id"] for item in task["notes"]] == [case.note["id"]]
    assert "library" not in task
    assert not any(path.startswith("library/") for path in files)
    assert str(case.root).encode() not in b"\n".join(files.values())


async def test_publication_removal_and_withdrawal_preserve_artifact_and_archive(archive_case):
    case = archive_case
    hub = case.hub
    artifact_id = case.candidate["artifact_id"]
    staged = await hub.artifact(artifact_id)
    assert staged["category"] == "artifact"
    assert Path(staged["content_file"]["path"]).is_relative_to(case.root / "staging")
    await approve(hub, case.reviewer)
    published = await hub.artifact(artifact_id)
    archive = Path(published["content_file"]["path"])
    assert published["status"] == "accepted"
    assert published["category"] == "artifact"
    assert archive.is_relative_to(case.root / "artifacts")
    assert "library_membership" not in published
    assert artifact_id in {item["id"] for item in await hub.list_math_artifacts()}
    receipt = (archive.parent / "publication.json").read_bytes()

    files, _ = await sync_state.export_files(hub, main_commit=(await hub.repository_status())["head"])
    assert [item["id"] for item in json.loads(files["artifacts/index.json"])] == [artifact_id]
    assert not any(path.startswith("library/") for path in files)

    await remove_current_pair(case)
    retained = await hub.artifact(artifact_id)
    assert retained["status"] == "accepted"
    assert retained["category"] == "artifact"
    for field in ("id", "kind", "body_sha", "snapshot", "data", "evidence", "content_file"):
        assert retained[field] == published[field]
    assert (archive.parent / "publication.json").read_bytes() == receipt
    await command(hub, "artifact.withdraw", {
        "artifact_id": artifact_id, "reason": "Synthetic withdrawal after source removal."})
    withdrawn = await hub.artifact(artifact_id)
    assert withdrawn["status"] == "withdrawn"
    assert withdrawn["category"] == "artifact"
    assert withdrawn["content_file"] == published["content_file"]
    assert withdrawn["evidence"] == published["evidence"]
    assert (archive.parent / "publication.json").read_bytes() == receipt
    assert (archive.parent / "files/Fixture.lean").read_bytes() == case.submitted
    assert artifact_id in {item["id"] for item in await hub.list_math_artifacts()}

    files, _ = await sync_state.export_files(hub, main_commit=(await hub.repository_status())["head"])
    indexed = json.loads(files["artifacts/index.json"])
    assert [item["id"] for item in indexed] == [artifact_id]
    assert indexed[0]["status"] == "withdrawn"
    task = json.loads(files[f"tasks/{case.host['id']}/task.json"])
    assert [item["id"] for item in task["artifacts"]] == [artifact_id]
    exported = json.loads(files[f"artifacts/{artifact_id}.json"])
    assert exported["category"] == "artifact"
    assert exported["body_sha"] == published["body_sha"]
    assert exported["evidence"] == sync_state._portable(published["evidence"])
    assert indexed[0]["path"] == f"artifacts/{artifact_id}.json"


@pytest.mark.parametrize("disposition", ["rejected", "withdrawn"])
async def test_nonpublished_history_retains_artifact_classification_and_discovery(archive_case, disposition):
    case = archive_case
    artifact_id = case.candidate["artifact_id"]
    original = await case.hub.artifact(artifact_id)
    if disposition == "rejected":
        await command(case.hub, "review.reject", decision(case, suggestions=[
            "Revise the paired fixture explanation before resubmitting."]), worker(case.reviewer))
        await quiet(case.hub, case.reviewer)
    else:
        await command(case.hub, "artifact.withdraw", {
            "artifact_id": artifact_id, "reason": "Synthetic unpublished fixture withdrawal."})
    await case.hub.archive_moves_settled()
    retained = await case.hub.artifact(artifact_id)
    assert retained["status"] == disposition
    assert retained["category"] == "artifact"
    assert Path(retained["content_file"]["path"]).is_relative_to(case.root / "submission-history")
    assert retained["body_sha"] == original["body_sha"]
    assert retained["snapshot"] == original["snapshot"]
    assert retained["data"] == original["data"]
    assert "library_membership" not in retained
    assert artifact_id in {item["id"] for item in await case.hub.list_math_artifacts()}
    files, _ = await sync_state.export_files(case.hub, main_commit=case.initial["head"])
    indexed = json.loads(files["artifacts/index.json"])
    assert [(item["id"], item["status"]) for item in indexed] == [(artifact_id, disposition)]
    assert not any(path.startswith("library/") for path in files)
