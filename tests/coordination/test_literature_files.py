"""Readable literature layout preserves exact citations and on-demand wiki reading."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.literature import INDEX
from formal_geyser.coordination.models import (
    Command,
    ProjectConfig,
    RepositoryConfig,
)
from formal_geyser.coordination.wiki import LIBRARY_GUIDE, LITERATURE_GUIDE
from formal_geyser.registry import ObjectRegistry
from formal_geyser.schemas import SourceLocation


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


async def register(hub, source, source_id="paper-v1", title="Author - Paper", **fields):
    return await command(hub, "literature.register", {
        "source_id": source_id, "title": title, "path": str(source), **fields})


@pytest.fixture
async def literature(tmp_path):
    root = tmp_path / "project"
    initialize(root, ProjectConfig())
    hub = Hub(root)
    yield hub
    await hub.close()


async def test_browsable_layout_keeps_central_pointer_and_readable_local_name(literature, tmp_path):
    central = tmp_path / "Central-Library"
    central.mkdir()
    paper = central / "Original.pdf"
    paper.write_bytes(b"Source fixture bytes")
    pointer = await register(literature, paper)
    copied = await register(literature, paper, source_id="retained-v1", title="Author - Retained", copy=True)
    root = literature.root / "literature"
    assert sorted(p.name for p in root.iterdir()) == ["Author - Retained.pdf", INDEX]
    assert pointer["source_path"] == str(paper)
    assert Path(copied["source_path"]) == root / "Author - Retained.pdf"
    assert (root / "Author - Retained.pdf").read_bytes() == paper.read_bytes()
    assert Path(copied["path"]).is_relative_to(literature.state_dir / "literature")
    index = (root / INDEX).read_text(encoding="utf-8")
    assert paper.as_posix() in index and "Author - Retained.pdf" in index
    assert copied["source_sha256"] not in index
    assert list(central.iterdir()) == [paper]


@pytest.mark.parametrize("mutation", ["change", "add", "remove", "rename"])
async def test_folder_citation_binds_complete_sources(literature, tmp_path, mutation):
    folder = tmp_path / "Publication"
    folder.mkdir()
    (folder / "paper.pdf").write_bytes(b"PDF fixture")
    source = folder / "source.tex"
    source.write_text("Source fixture", encoding="utf-8")
    entry = await register(literature, folder)
    assert "files" not in entry
    assert len(json.loads(Path(entry["path"]).read_text(encoding="utf-8"))["files"]) == 2
    reference = SourceLocation(literature_id=entry["id"], content_sha256=entry["source_sha256"],
                               locator="source.tex, Section 2")
    evidence = await ObjectRegistry(literature).source_evidence(reference)
    assert evidence["source_kind"] == "directory" and evidence["entry_path"] == entry["path"]
    assert "[folder]" in (literature.root / "literature" / INDEX).read_text(encoding="utf-8")
    if mutation == "change":
        source.write_text("A different edition", encoding="utf-8")
    elif mutation == "add":
        (folder / "extra.tex").write_text("Added source", encoding="utf-8")
    elif mutation == "remove":
        source.unlink()
    else:
        source.rename(folder / "renamed.tex")
    with pytest.raises(HubError, match="changed"):
        await literature.accessible_literature(entry["id"])


async def test_folder_import_and_name_collision_never_overwrite_sources(literature, tmp_path):
    folder = tmp_path / "download"
    (folder / "figures").mkdir(parents=True)
    (folder / "paper.tex").write_text("Edition one", encoding="utf-8")
    (folder / "figures" / "diagram.txt").write_text("Figure fixture", encoding="utf-8")
    first = await register(literature, folder, copy=True, uri="https://example.test/paper")
    again = await register(literature, folder)
    assert again["source_path"] == first["source_path"] and again["uri"] == first["uri"]
    (folder / "paper.tex").write_text("Edition two", encoding="utf-8")
    with pytest.raises(HubError, match="new source ID"):
        await register(literature, folder, copy=True)
    second = await register(literature, folder, source_id="paper-v2", copy=True)
    assert Path(first["source_path"]).name == "Author - Paper"
    assert Path(second["source_path"]).name == "Author - Paper -- paper-v2"
    assert (Path(first["source_path"]) / "paper.tex").read_text() == "Edition one"
    assert (Path(second["source_path"]) / "figures" / "diagram.txt").is_file()
    assert not list((literature.root / "literature").glob(".import-*"))


async def test_concurrent_registration_preserves_manual_index_notes(literature, tmp_path):
    index = literature.root / "literature" / INDEX
    index.parent.mkdir()
    index.write_text("# Selected references\n\nKeep this personal reading note.\n", encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("Stable source", encoding="utf-8")
    await asyncio.gather(*(register(literature, source, source_id=f"source-{i}") for i in range(4)))
    await register(literature, source, source_id="source-0")
    body = index.read_text(encoding="utf-8")
    assert body.startswith("# Selected references\n\nKeep this personal reading note.\n")
    for i in range(4):
        assert body.count(f"<!-- literature:source-{i} -->") == 1


async def test_registration_retry_recovers_files_after_rolled_back_index(literature, tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"Stable publication")
    payload = {"source_id": "retried", "title": "Retained paper", "path": str(source), "copy": True}
    def aborted(c):
        literature._register_literature(c, payload)
        raise RuntimeError("Commit interrupted")
    with pytest.raises(RuntimeError):
        await literature.db.call(aborted, write=True)
    assert await literature.list_literature() == []
    entry = await command(literature, "literature.register", payload)
    assert (await literature.accessible_literature(entry["id"]))["source_sha256"] == entry["source_sha256"]
    assert (literature.root / "literature" / INDEX).read_text().count("<!-- literature:retried -->") == 1


async def test_local_citation_survives_project_copy(tmp_path):
    source = tmp_path / "download.pdf"
    source.write_bytes(b"Stable fixture")
    root = tmp_path / "first"
    initialize(root, ProjectConfig())
    hub = Hub(root)
    try:
        entry = await register(hub, source, copy=True)
        metadata = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
        assert metadata["source_path"] == "literature/Author - Paper.pdf"
    finally:
        await hub.close()
    second = tmp_path / "second"
    shutil.copytree(root, second)
    reopened = Hub(second)
    try:
        restored = await reopened.accessible_literature(entry["id"])
        assert Path(restored["source_path"]) == second / metadata["source_path"]
        assert restored["source_sha256"] == entry["source_sha256"]
    finally:
        await reopened.close()


@pytest.mark.parametrize("repository", [False, True])
@pytest.mark.parametrize("edited_guide", [LITERATURE_GUIDE, LIBRARY_GUIDE])
async def test_literature_guides_are_direct_on_demand_files(tmp_path, repository, edited_guide):
    root = tmp_path / "project"
    initialize(root, ProjectConfig(domain="lean", repository=RepositoryConfig(enabled=repository)))
    from tests.bootstrap_helpers import synthetic_execution
    hub = synthetic_execution(Hub(root))
    try:
        if repository:
            await command(hub, "repository.initialize")
        await command(hub, "task.publish", {"contract": {"title": "Documentation fixture", "domain": "lean",
            "acceptance_policy": "lean.peer@1", "objective": "Read relevant source procedures."}})
        run = await command(hub, "run.start")
        assignment = await hub.next_assignment(run["id"])
        native = await hub.prepare_native_context(assignment["worker_id"])
        assert Path(native["directories"]["literature_index"]) == root / "literature" / INDEX
        assert Path(native["directories"]["literature_wiki"]) == root / LITERATURE_GUIDE
        assert Path(native["directories"]["library_wiki"]) == root / LIBRARY_GUIDE
        (root / edited_guide).write_text("# Updated source procedure\n", encoding="utf-8")
        refreshed = await hub.prepare_native_context(assignment["worker_id"])
        assert refreshed == native
        assert not (hub.workspace(assignment["worker_id"]) / ".private/wiki-versions").exists()
    finally:
        await hub.close()
