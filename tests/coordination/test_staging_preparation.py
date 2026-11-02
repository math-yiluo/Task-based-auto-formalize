"""A staged submission's snapshot files are written before its transaction, off the coordination thread."""
import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from formal_geyser.coordination import artifact_files
from formal_geyser.coordination.artifact_files import ArtifactFiles
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.content import FreshTree
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import ProjectConfig, RepositoryConfig
from formal_geyser.coordination.runtime.native_files import _record_file
from tests.coordination.test_archive_publication import command, manifest, quiet, worker

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Native Git is required")


SUPPORT = {"Support/Other.lean": b"-- Unrelated snapshot file.\n", "Support/Other.md": b"Unrelated explanation.\n"}


@pytest.fixture
async def submitted(tmp_path):
    """An author whose paired Lean/Markdown submission waits for its quiet snapshot."""
    async for case in submission(tmp_path, {}):
        yield case


@pytest.fixture
async def submitted_over_library(tmp_path):
    """The same over a Library that already has the unrelated support pair."""
    async for case in submission(tmp_path, SUPPORT):
        yield case


async def submission(tmp_path, library):
    initialize(tmp_path, ProjectConfig(repository=RepositoryConfig(enabled=True)))
    hub = Hub(tmp_path)
    case = SimpleNamespace(hub=hub, root=tmp_path)
    try:
        await command(hub, "repository.initialize", {"snapshot": manifest(hub, library)})
        await command(hub, "task.publish", {"contract": {
            "title": "Staging fixture", "objective": "Stage exact paired files.",
            "acceptance_policy": "general.peer@1"}})
        run = await command(hub, "run.start")
        case.author = await hub.next_assignment(run["id"])
        lean, markdown = b"-- Staging fixture; no proof is asserted.\n", b"Staging fixture explanation.\n"
        root = hub.workspace(case.author["worker_id"])
        (root / "Fixture.lean").write_bytes(lean)
        (root / "Fixture.md").write_bytes(markdown)
        for path, body in SUPPORT.items():
            (root / path).parent.mkdir(exist_ok=True)
            (root / path).write_bytes(body)
        await command(hub, "artifact.submit", {"kind": "lean.statement",
            "repository_paths": ["Fixture.lean", "Fixture.md"], "data": {"version": 1,
            "bundle": {"lean_path": "Fixture.lean", "markdown_path": "Fixture.md",
                       "lean_sha256": hub.blobs.put(lean), "markdown_sha256": hub.blobs.put(markdown)}}},
            worker(case.author))
        yield case
    finally:
        await hub.close()


def leftovers(case):
    return list((case.root / "staging").glob(".native-content-*"))


async def test_only_the_record_and_manifest_are_written_inside_the_staging_transaction(submitted, monkeypatch):
    case = submitted
    writes = []
    original, fresh = artifact_files._write_new, FreshTree.write

    def observed(path, body, **options):
        writes.append((threading.current_thread().name, Path(path).parts))
        return original(path, body, **options)

    def observed_fresh(tree, name, body, **options):
        writes.append((threading.current_thread().name, (tree.root / name).parts))
        return fresh(tree, name, body, **options)
    monkeypatch.setattr(artifact_files, "_write_new", observed)
    monkeypatch.setattr(FreshTree, "write", observed_fresh)
    candidate = await quiet(case.hub, case.author)
    on_thread = {parts[-1] for name, parts in writes if name.startswith("taskhub-db")}
    assert on_thread == {"record.json", "manifest.json"}
    assert any("files" in parts for name, parts in writes if not name.startswith("taskhub-db"))
    record = await case.hub.artifact(candidate["artifact_id"])
    directory = Path(record["content_file"]["path"]).parent
    body_sha = await case.hub.db.read(lambda c: c.execute(
        "SELECT body_sha FROM artifacts WHERE id=?", (record["id"],)).fetchone()[0])
    archive = ArtifactFiles(case.hub.content).verify(directory, record["id"], 1, body_sha)
    assert {item["path"] for item in archive["files"]} == {  # Over an empty Library everything differs.
        "record.json", "base.json", "files/Fixture.lean", "files/Fixture.md", "files/Support/Other.lean",
        "files/Support/Other.md"}
    assert leftovers(case) == []


async def test_an_archive_holds_only_what_differs_from_its_library_base(submitted_over_library):
    case = submitted_over_library
    candidate = await quiet(case.hub, case.author)
    record = await case.hub.artifact(candidate["artifact_id"])
    directory = Path(record["content_file"]["path"]).parent

    def recorded(c):
        row = c.execute("SELECT body_sha,snapshot FROM artifacts WHERE id=?", (record["id"],)).fetchone()
        return tuple(row), c.execute("SELECT base_commit FROM candidate_repositories WHERE artifact_id=?",
                                     (record["id"],)).fetchone()[0]
    (body_sha, snapshot), base_commit = await case.hub.db.read(recorded)
    archive = ArtifactFiles(case.hub.content).verify(directory, record["id"], 1, body_sha)
    assert {item["path"] for item in archive["files"]} == {
        "record.json", "base.json", "files/Fixture.lean", "files/Fixture.md"}
    base = json.loads((directory / "base.json").read_text(encoding="utf-8"))
    assert (base["snapshot"], base["base_commit"], base["removed"]) == (snapshot, base_commit, [])
    await case.hub.public_content()  # The base commit's published view.
    staging = case.root.resolve() / "staging"

    def read(relative):
        return case.hub.db.read(lambda c: _record_file(case.hub, c, None, directory / relative, staging))
    files, name = await read("files/Support/Other.lean")
    assert files.read(name)["text"] == SUPPORT["Support/Other.lean"].decode()  # Unchanged: the base's bytes.
    with pytest.raises(HubError, match="not a frozen file"):
        await read("files/Absent.lean")
    await command(case.hub, "artifact.withdraw", {"artifact_id": record["id"], "reason": "Moving the archive."})
    await case.hub.archive_moves_settled()
    moved = Path((await case.hub.artifact(record["id"]))["content_file"]["path"]).parent
    assert moved != directory and {item["path"] for item in json.loads((moved / "manifest.json").read_text(
        encoding="utf-8"))["files"]} == {"record.json", "base.json", "files/Fixture.lean", "files/Fixture.md",
                                         "history.json"}


async def test_prepared_files_are_removed_when_staging_fails(submitted, monkeypatch):
    case = submitted

    def refused(*args, **kwargs):
        raise HubError("SYNTHETIC_REFUSAL", "Synthetic staging refusal")
    monkeypatch.setattr(type(case.hub), "_stage_candidate", refused)
    with pytest.raises(HubError, match="Synthetic staging refusal"):
        await quiet(case.hub, case.author)
    assert leftovers(case) == []


async def test_prepared_files_changed_before_the_transaction_are_not_staged(submitted, monkeypatch):
    case = submitted
    original = ArtifactFiles.prepare_sources

    def tampered(self, blobs, snapshot, base=None):
        prepared = original(self, blobs, snapshot, base)
        (prepared.directory / "files" / "Unexpected.lean").write_bytes(b"-- Not in the snapshot.\n")
        return prepared
    monkeypatch.setattr(ArtifactFiles, "prepare_sources", tampered)
    with pytest.raises(HubError):
        await quiet(case.hub, case.author)
    assert leftovers(case) == []
    assert not [path for path in (case.root / "staging").iterdir() if (path / "1" / "manifest.json").exists()]
