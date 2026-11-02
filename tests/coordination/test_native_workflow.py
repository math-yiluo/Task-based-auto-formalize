"""Native workflow authority, privacy, and direct public knowledge paths without live workers."""
from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination import sync_state
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.content import ContentError
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    Limits,
    ProjectConfig,
    RepositoryConfig,
    Role,
)
from formal_geyser.coordination.repository import RepositoryStore

from .lifecycle_helpers import open_collaboration, select_review
from .publication_helpers import approve, file_payload

OPERATOR = Actor(role=Role.OPERATOR)
RUNNER = Actor(role=Role.RUNNER)
VALIDATOR = Actor(role=Role.VALIDATOR)
CHECKPOINT = {"summary": "Synthetic native workflow checkpoint.", "next_action": "Continue the same task team."}


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


def worker(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


def snapshot(hub, files):
    return hub.blobs.put_json({"files": [
        {"path": path, "sha256": hub.blobs.put(body)} for path, body in sorted(files.items())
    ]})


async def quiet(hub, attempt):
    return await command(hub, "runtime.quiescent", {
        "worker_id": attempt["worker_id"],
        "snapshot": hub.blobs.snapshot(hub.workspace(attempt["worker_id"])),
    }, RUNNER)


@pytest.fixture
async def native_case(tmp_path_factory):
    hubs = []

    async def create(*, repository=False, files=None):
        if repository and shutil.which("git") is None:
            pytest.skip("Local Git is required")
        root = tmp_path_factory.mktemp("nw")
        initialize(root, ProjectConfig(repository=RepositoryConfig(enabled=repository), limits=Limits()))
        hub = Hub(root)
        hubs.append(hub)
        case = SimpleNamespace(hub=hub, root=root)
        if repository:
            case.initial = await command(hub, "repository.initialize", {"snapshot": snapshot(hub, files or {})})
        case.host = await command(hub, "task.publish", {"contract": {
            "title": "Native document workflow", "objective": "# Objective\nInspect synthetic software documents.\n",
            "context": "Use the exact published wiki before exploring.\n", "acceptance_policy": "general.peer@1",
        }})
        case.run = await command(hub, "run.start")
        case.author = await hub.next_assignment(case.run["id"])
        return case

    yield create
    for hub in hubs:
        await hub.close()


async def another_worker(case):
    await open_collaboration(case.hub, case.host["id"])
    registered = await command(case.hub, "worker.register")
    return await command(case.hub, "task.claim", {"task_id": case.host["id"], "revision": 1,
                         "worker_id": registered["id"], "run_id": case.run["id"],
                         "plan": "Inspect the native document fixture independently."})


async def publish_wiki(case, changes):
    """Use the actual independent review protocol for a synthetic wiki change."""
    hub = case.hub
    for path, body in changes.items():
        target = hub.workspace(case.author["worker_id"]) / path
        if body is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    await command(hub, "artifact.submit", {"kind": "workload.note", "data": {"summary": "Checked wiki edits."},
                  "repository_paths": sorted(changes)}, worker(case.author))
    staged = await quiet(hub, case.author)
    await command(hub, "artifact.accept", {"artifact_id": staged["artifact_id"], "policy": "general.peer@1",
                  "verdict": "accepted", "details": {"scope": "Synthetic original document check"}}, VALIDATOR)
    candidate = await hub.artifact(staged["artifact_id"])
    await approve(hub, await select_review(hub, candidate["review_task_id"], case.run["id"]))
    assert (await hub.artifact(staged["artifact_id"]))["accepted"]
    case.author = await command(hub, "task.claim", {"task_id": case.host["id"], "revision": 1,
                                "worker_id": case.author["worker_id"], "run_id": case.run["id"]})
    return {"commit": (await hub.repository_status())["head"]}


@pytest.mark.parametrize("change", ["modify", "remove"])
@pytest.mark.parametrize("name", ["task.json", "objective.md"])
async def test_task_files_remain_authoritative_when_sql_cache_is_intact(native_case, change, name):
    case = await native_case()
    hub, task = case.hub, case.host
    reference = task["files"]
    root = Path(reference["root"])
    assert Path(reference["path"]) == root / "task.json"
    assert (root / "objective.md").read_bytes() == task["contract"]["objective"].encode()
    assert (root / "context.md").read_bytes() == task["contract"]["context"].encode()
    assert json.loads((root / "task.json").read_bytes())["objective"] == "objective.md"
    cached = await hub.db.call(lambda c: c.execute("SELECT contract_json FROM task_revisions WHERE task_id=? AND revision=1",
                              (task["id"],)).fetchone()[0])
    target = root / name
    target.chmod(stat.S_IREAD | stat.S_IWRITE)
    if change == "modify":
        target.write_bytes(b"Unexpected external edit.\n")
    else:
        target.unlink()
    with pytest.raises((ContentError, HubError, FileNotFoundError)):
        await hub.task(task["id"])
    assert await hub.db.call(lambda c: c.execute("SELECT contract_json FROM task_revisions WHERE task_id=? AND revision=1",
                             (task["id"],)).fetchone()[0]) == cached
    assert json.loads(cached)["objective"] == task["contract"]["objective"]
    if change == "modify":
        assert target.read_bytes() == b"Unexpected external edit.\n"
    else:
        assert not target.exists()


@pytest.mark.parametrize("change", ["modify", "remove"])
async def test_record_file_is_authority_without_a_blob_body_and_never_falls_back(native_case, change):
    case = await native_case(repository=True)
    hub = case.hub
    data = {"summary": "Native record body with exact content."}
    record = await command(hub, "artifact.stage", file_payload(
        hub, case.author, kind="workload.note", data=data), worker(case.author))
    target = Path(record["content_file"]["path"])
    assert record["content_file"]["category"] == "note" and json.loads(target.read_bytes()) == data
    assert (await hub.artifact(record["id"]))["data"] == data
    with pytest.raises(FileNotFoundError):
        hub.blobs.get(record["body_sha"])
    # Even a valid historical/cache copy must not repair or override the file.
    assert hub.blobs.put_json(data) == record["body_sha"]
    target.chmod(stat.S_IREAD | stat.S_IWRITE)
    if change == "modify":
        target.write_bytes(b'{"summary":"Changed outside the immutable record."}\n')
    else:
        target.unlink()
    with pytest.raises((ContentError, HubError, FileNotFoundError)):
        await hub.artifact(record["id"])
    assert hub.blobs.get_json(record["body_sha"]) == data
    assert target.exists() is (change == "modify")


@pytest.mark.parametrize("change", [None, "modify", "remove"])
async def test_mathematical_candidate_pair_is_inspectable_without_claiming_acceptance(native_case, change):
    case = await native_case(repository=True)
    hub = case.hub
    lean = b"-- Synthetic source metadata fixture; no theorem or proof is asserted.\n"
    markdown = b"# Candidate source fixture\nNo mathematical acceptance claim.\n"
    workspace = hub.workspace(case.author["worker_id"])
    (workspace / "Candidate.lean").write_bytes(lean)
    (workspace / "Candidate.md").write_bytes(markdown)
    record = await command(hub, "artifact.stage", {"kind": "lean.statement", "repository_paths": ["Candidate.lean", "Candidate.md"], "data": {
        "name": "Synthetic candidate pair", "bundle": {
            "lean_path": "Candidate.lean", "markdown_path": "Candidate.md",
            "lean_sha256": hub.blobs.put(lean), "markdown_sha256": hub.blobs.put(markdown),
        },
    }}, worker(case.author))
    target = Path(record["content_file"]["path"])
    assert record["content_file"]["category"] == "artifact"
    assert (target.parent / "files/Candidate.lean").read_bytes() == lean
    assert (target.parent / "files/Candidate.md").read_bytes() == markdown
    assert not record["accepted"] and not record["public"]
    assert record["id"] in {item["id"] for item in await hub.list_math_artifacts()}
    assert record["id"] not in {item["id"] for item in await hub.list_notes()}
    if change is not None:
        lean_file = target.parent / "files/Candidate.lean"
        lean_file.chmod(stat.S_IREAD | stat.S_IWRITE)
        if change == "modify":
            lean_file.write_bytes(b"-- Changed after freezing.\n")
        else:
            lean_file.unlink()
        with pytest.raises((ContentError, HubError, FileNotFoundError)):
            await hub.artifact(record["id"])
        assert hub.blobs.get(record["data"]["bundle"]["lean_sha256"]) == lean
        assert lean_file.exists() is (change == "modify")


async def test_private_runtime_diagnostics_stay_owned_and_out_of_shared_views(native_case):
    case = await native_case(repository=True)
    hub = case.hub
    secret = "PrivateNativeNoteSentinel"
    note = await command(hub, "record.private", {"kind": "tool.diagnostic", "data": {"summary": secret}}, worker(case.author))
    target = Path(note["content_file"]["path"])
    assert target.is_relative_to(hub.workspace(case.author["worker_id"]) / ".private")
    assert note["content_file"]["owner_id"] == case.author["worker_id"]
    assert (await hub.native_record(note["id"], worker(case.author)))["data"]["summary"] == secret
    other = await another_worker(case)
    assert (await hub.native_record(note["id"], worker(other)))["data"]["summary"] == secret
    assert await hub.search(secret) == []
    assert note["id"] not in {item["id"] for item in await hub.list_notes()}
    exported, _ = await sync_state.export_files(hub, main_commit=case.initial["head"])
    assert all(secret.encode() not in body for body in exported.values())
    assert all(not path.startswith(f"artifacts/{note['id']}") for path in exported)
    assert all(".private" not in item["path"].upper().casefold().split("/")
               for item in hub.blobs.get_json(hub.blobs.snapshot(hub.workspace(case.author["worker_id"]))) ["files"])


async def test_direct_private_markdown_does_not_register_or_publish_a_record(native_case):
    case = await native_case()
    hub = case.hub
    before = await hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0])
    notes = hub.content.worker_notes(case.author["worker_id"], hub.workspace(case.author["worker_id"]))
    target = notes / "scratch.md"
    target.write_bytes(b"UnregisteredNativeMarkdownSentinel\n")
    assert await hub.db.call(lambda c: c.execute("SELECT count(*) FROM artifacts").fetchone()[0]) == before
    assert await hub.search("UnregisteredNativeMarkdownSentinel") == []
    native = await hub.prepare_native_context(case.author["worker_id"])
    assert Path(native["private_notes"]) == notes
    assert target.read_bytes() == b"UnregisteredNativeMarkdownSentinel\n"
    assert all(".private" not in item["path"].upper().casefold().split("/")
               for item in hub.blobs.get_json(hub._candidate_snapshot(case.author["worker_id"]))["files"])


async def test_public_content_uses_only_recorded_public_head(native_case):
    case = await native_case(repository=True, files={"wiki/guide.md": b"# Accepted initial guide\n"})
    hub = case.hub
    public = await hub.public_content()
    assert public.commit == case.initial["head"]
    assert public.root == case.root / "library" / public.commit / "files"
    assert (public.root / "wiki/guide.md").read_bytes() == b"# Accepted initial guide\n"
    repository = RepositoryStore(hub.state_dir)
    workspace = hub.workspace(case.author["worker_id"]) / ".taskhub-integration/unrecorded"
    repository.prepare(hub.blobs, snapshot(hub, {"wiki/guide.md": b"# Unaccepted replacement\n"}),
                       public.commit, workspace, ["wiki/guide.md"])
    frozen = repository.finalize(workspace, hub.blobs)
    assert (await hub.public_content()).commit == public.commit
    repository.publish(frozen["commit"], public.commit)
    with pytest.raises(HubError) as error:
        await hub.public_content()
    assert error.value.code == "REPOSITORY_RECONCILIATION_REQUIRED"
    assert (public.root / "wiki/guide.md").read_bytes() == b"# Accepted initial guide\n"


async def test_knowledge_paths_follow_publication_without_page_receipts(native_case):
    case = await native_case(repository=True, files={"wiki/README.md": b"# Public knowledge\n",
        "notes/intro.md": b"# Initial note\n"})
    hub = case.hub
    first = await hub.prepare_native_context(case.author["worker_id"])
    assert first["library"]["commit"] == case.initial["head"]
    assert Path(first["directories"]["knowledge_readme"]).name == "README.md"
    frozen = await publish_wiki(case, {"notes/intro.md": b"# Updated note\n"})
    next_input = await hub.prepare_native_context(case.author["worker_id"])
    assert next_input["library"]["commit"] == frozen["commit"]
    assert not {"pages", "required_wiki", "removed"} & next_input.keys()
    await command(hub, "worker.compacted", {"worker_id": case.author["worker_id"]}, RUNNER)
    compacted = await hub.prepare_native_context(case.author["worker_id"])
    assert not {"pages", "required_wiki", "full_read"} & compacted.keys()


@pytest.mark.parametrize("repository", [False, True])
async def test_empty_library_has_starter_readme_and_no_reading_tables(native_case, repository):
    case = await native_case(repository=repository)
    native = await case.hub.prepare_native_context(case.author["worker_id"])
    assert Path(native["directories"]["knowledge_readme"]).is_file()
    tables = await case.hub.db.call(lambda c: {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")})
    assert not {"wiki_readings", "wiki_deliveries"} & tables


async def test_shared_export_reads_task_files_instead_of_stale_revision_cache(native_case):
    case = await native_case(repository=True)
    hub = case.hub
    stale = {**case.host["contract"], "objective": "A stale derived SQL cache must not become shared content."}
    await hub.db.call(lambda c: c.execute("UPDATE task_revisions SET contract_json=? WHERE task_id=? AND revision=1",
                     (json.dumps(stale), case.host["id"])), write=True)
    assert (await hub.task(case.host["id"]))["contract"] == case.host["contract"]
    exported, _ = await sync_state.export_files(hub, main_commit=case.initial["head"])
    revisions = json.loads(exported["task-revisions.json"])
    revision = next(item for item in revisions if item["task_id"] == case.host["id"] and item["revision"] == 1)
    assert revision["contract"] == case.host["contract"]
