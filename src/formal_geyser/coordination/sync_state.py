"""Portable, read-only views of the shared task ledger for ordinary Git clients."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from .errors import HubError
from .git_transport import STATE_DIRECTORY
from .storage import canonical

_SCHEMA = """
CREATE TABLE IF NOT EXISTS git_sync_state(
 id INTEGER PRIMARY KEY CHECK(id=1),project_id TEXT NOT NULL,remote TEXT,
 main_head TEXT,pending_digest TEXT,last_export_digest TEXT,
 last_export_sequence INTEGER NOT NULL DEFAULT 0,last_success_at INTEGER,last_error TEXT,
 pending_head TEXT);
"""


def _ensure(c):
    # executescript implicitly commits on some Python/SQLite configurations.
    # Keep schema creation inside the caller's ordinary transaction instead.
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            c.execute(statement)
    c.execute("INSERT OR IGNORE INTO git_sync_state(id,project_id) VALUES(1,?)", (uuid4().hex,))


async def state(hub) -> dict:
    def read(c):
        _ensure(c)
        return dict(c.execute("SELECT * FROM git_sync_state WHERE id=1").fetchone())
    return await hub.db.call(read, write=True)


async def update_state(hub, **fields) -> dict:
    allowed = {"remote", "main_head", "pending_head", "pending_digest", "last_export_digest",
               "last_export_sequence", "last_success_at", "last_error"}
    if not fields or set(fields) - allowed:
        raise ValueError("Invalid synchronization state update")

    def write(c):
        _ensure(c)
        c.execute("UPDATE git_sync_state SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=1",
                  tuple(fields.values()))
        return dict(c.execute("SELECT * FROM git_sync_state WHERE id=1").fetchone())
    return await hub.db.call(write, write=True)


def file_digest(contents: dict[str, bytes]) -> str:
    manifest = {path: hashlib.sha256(body).hexdigest() for path, body in sorted(contents.items())}
    return hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _event_view(row) -> dict:
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item


def _portable(value: Any) -> Any:
    """Keep shared metadata; local native and execution locations are not cloud resources."""
    local = {"content_file", "workspace", "workspace_id", "workspace_relative_path",
             "private_notes", "checkpoint_json", "profile_json", "codex_profile", "native",
             "peer_talk", "talk", "drafts"}
    if isinstance(value, dict):
        return {key: item if key in {"data", "contract", "details"} else _portable(item)
                for key, item in value.items() if key not in local}
    if isinstance(value, list):
        return [_portable(item) for item in value]
    return value


def _public_event(row, private_ids: set[str]) -> bool:
    return (row["object_id"] not in private_ids
            and row["kind"] not in {"record.private", "draft.updated", "dags.updated"}
            and not row["kind"].startswith(("talk.", "peer_talk.", "peer.")))


def _record_index(record: dict, path: str) -> dict:
    return {**{key: record.get(key) for key in
               ("id", "kind", "category", "task_id", "task_revision", "status", "body_sha", "snapshot")},
            "path": path}


async def export_files(hub, *, main_commit: str) -> tuple[dict[str, bytes], int]:
    """Capture one database revision. Never read credentials or mutable workspaces.

    This is a collaboration export, not an executable process checkpoint. Public
    file contents live beside taskhub/ on the public branch; the ledger exports
    hashes and declared records rather than private workspace blobs.
    """
    public = await hub.public_content()
    if public is None or public.commit != main_commit:
        raise HubError("SYNC_SOURCE_MOVED", "The publication changed while preparing its shared state; retry sync")

    def capture(c):
        _ensure(c)
        current = hub._require_recorded_repository_head(c, hub._repository())
        if current != main_commit:
            raise HubError("SYNC_SOURCE_MOVED", "The publication changed while preparing its shared state; retry sync")
        metadata = dict(c.execute("SELECT * FROM git_sync_state WHERE id=1").fetchone())
        private_ids = {row["id"] for row in c.execute(
            "SELECT a.id FROM artifacts a LEFT JOIN native_record_files n ON n.record_id=a.id "
            "WHERE a.status='private' OR n.owner_id IS NOT NULL "
            "OR a.id IN (SELECT artifact_id FROM private_records)")}
        events = [_portable(_event_view(row)) for row in c.execute(
            "SELECT * FROM events ORDER BY sequence").fetchall() if _public_event(row, private_ids)]
        last = {"sequence": max((item["sequence"] for item in events), default=0),
                "updated_at": max((item["created_at"] for item in events), default=0)}
        artifacts = [_portable(hub._artifact(c, row["id"])) for row in c.execute(
            "SELECT id FROM artifacts WHERE status<>'private' AND id NOT IN "
            "(SELECT artifact_id FROM private_records) AND id NOT IN "
            "(SELECT record_id FROM native_record_files WHERE owner_id IS NOT NULL) "
            "ORDER BY created_at,id").fetchall()]
        by_id = {item["id"]: item for item in artifacts}
        tasks = []
        for row in c.execute("SELECT id FROM tasks ORDER BY created_at,id").fetchall():
            task = hub._task(c, row["id"])
            task.pop("files", None)
            task["active_attempts"] = [{key: attempt.get(key) for key in
                                        ("id", "task_id", "task_revision", "worker_id", "run_id",
                                         "state", "created_at", "ended_at")}
                                       for attempt in task["active_attempts"]]
            shared = [by_id[item["id"]] for item in task["artifacts"] if item["id"] in by_id]
            for category, key in (("artifact", "artifacts"), ("note", "notes")):
                task[key] = [{field: item.get(field) for field in
                              ("id", "kind", "category", "status", "task_revision")}
                             for item in shared if item["category"] == category]
            task["resolved_requirement_artifacts"] = hub._requirement_artifacts(c, task["contract"]["requirements"])
            tasks.append(_portable(task))
        revisions = [{**dict(row), "contract": hub._task_contract(c, row)} for row in c.execute(
            "SELECT * FROM task_revisions ORDER BY task_id,revision").fetchall()]
        for revision in revisions:
            revision.pop("contract_json")
        checkpoints = [dict(row) for row in c.execute(
            "SELECT id,task_id,task_revision,worker_id,state,created_at,ended_at FROM attempts "
            "WHERE checkpoint_json IS NOT NULL ORDER BY created_at,id").fetchall()]
        runs = [{key: row[key] for key in ("id", "state", "turns_used", "tokens_used", "usage_uncertain", "created_at")}
                for row in c.execute("SELECT * FROM runs ORDER BY created_at,id").fetchall()]
        return metadata, last, tasks, artifacts, events, revisions, checkpoints, runs

    (metadata, last, tasks, artifacts, events, revisions, checkpoints, runs) = await hub.db.call(capture, write=True)
    result: dict[str, bytes] = {}
    marker = {"format": "taskhub-git-export", "project_id": metadata["project_id"],
              "project_name": hub.config.name, "domain": hub.config.domain,
              "ledger_sequence": last["sequence"], "ledger_updated_at": last["updated_at"],
              "main_commit": main_commit, "main_branch": hub.config.repository.branch,
              "state_directory": STATE_DIRECTORY,
              "scope": "Shared knowledge, mathematical artifacts throughout their lifecycle, and public file references; private notes and peer conversations are excluded.",
              "content_indexes": {"notes": "notes/index.json", "artifacts": "artifacts/index.json",
                                  "knowledge": "knowledge/files.json"}}
    result["sync.json"] = _json(marker)
    result["runs.json"] = _json(runs)
    result["task-revisions.json"] = _json(revisions)
    result["checkpoints.json"] = _json(checkpoints)
    # Shards keep growth out of one continually rewritten large JSONL file.
    for offset in range(0, len(events), 500):
        group = events[offset:offset + 500]
        result[f"events/{group[0]['sequence']:012d}.jsonl"] = (
            "\n".join(canonical(item) for item in group) + "\n").encode("utf-8")
    result["artifacts/index.json"] = _json([
        _record_index(item, f"artifacts/{item['id']}.json") for item in artifacts
        if item["category"] == "artifact"])
    notes = [item for item in artifacts if item["category"] == "note"]
    result["notes/index.json"] = _json([
        _record_index(item, f"notes/{item['id']}.json") for item in notes])
    for note in notes:
        result[f"notes/{note['id']}.json"] = _json(note)
    for artifact in artifacts:
        if artifact["category"] != "artifact":
            continue
        result[f"artifacts/{artifact['id']}.json"] = _json(artifact)
    result["knowledge/files.json"] = _json([item.as_dict() for item in public.knowledge])
    result["knowledge/current.json"] = _json({"commit": main_commit,
        "branch": hub.config.repository.branch, "entrypoint": "wiki/README.md"})
    reference_paths = {item["id"]: (f"notes/{item['id']}.json" if item["category"] == "note"
                                   else f"artifacts/{item['id']}.json") for item in artifacts}
    index = ["# TaskHub tasks", "", f"Code baseline: `{main_commit}`.", "",
             "Read each task's context and exact revision before working. This directory is an exported view; do not edit it.",
             "", "| Task | Revision | State | Objective |", "| --- | --- | --- | --- |"]
    for task in tasks:
        tid = task["id"]
        result[f"tasks/{tid}/task.json"] = _json(task)
        contract = task["contract"]
        index.append(f"| [{_cell(contract['title'])}]({tid}/context.md) | {task['revision']} | {_cell(task['state'])} | {_cell(contract['objective'])} |")
        refs = sorted(set(contract["inputs"]) | set(task["resolved_requirement_artifacts"]))
        context = [f"# {contract['title']}", "", f"Task: `{tid}`; revision: `{task['revision']}`.",
                   f"Code baseline: `{main_commit}`.", "", "## Objective", "", contract["objective"],
                   "", "## Supplied context", "", contract.get("context") or "No additional context supplied.",
                   "", "## Constraints", "", *(f"- {value}" for value in contract["constraints"]),
                   "", "## Acceptance", "", f"Policy: `{contract['acceptance_policy']}`.",
                   "Acceptance and task completion require their recorded validation and review boundaries.",
                   "", "## Exact available inputs", "",
                   *(f"- [{ref}](../../{reference_paths[ref]})" for ref in refs if ref in reference_paths),
                   "", "Use exact note, candidate, and accepted Git references. Private local paths are not cloud resources.",
                   "", "See task.json for the full contract, prerequisites, current state, and publication history."]
        result[f"tasks/{tid}/context.md"] = ("\n".join(context) + "\n").encode("utf-8")
        timeline = [event for event in events if event["task_id"] == tid]
        result[f"tasks/{tid}/records.json"] = _json(timeline)
        progress = [f"# Task progress: {tid}", "", f"Current state: `{task['state']}`.", "",
                    "The timeline retains recorded task and publication events.", ""]
        for event in timeline[-100:]:
            summary = event["payload"].get("summary", "")
            progress.extend([f"- Event {event['sequence']}: `{event['kind']}`; object `{event['object_id']}`. {_cell(summary)}"])
        progress.extend(["", "See records.json for the complete recorded timeline."])
        result[f"tasks/{tid}/progress.md"] = ("\n".join(progress) + "\n").encode("utf-8")
    result["tasks/index.md"] = ("\n".join(index) + "\n").encode("utf-8")
    result["README.md"] = (
        "# Shared TaskHub state\n\nRead [the task index](tasks/index.md), "
        "[shared notes](notes/index.json), [mathematical artifacts and their history](artifacts/index.json), "
        "and [public knowledge](../wiki/README.md).\n\n"
        "This directory is generated by the local TaskHub. "
        "Reviewed source and public knowledge are in the repository root, alongside taskhub/. "
        "Make changes through the local project's review and publication workflow. "
        "Do not edit generated taskhub/ files.\n"
    ).encode("utf-8")
    return result, int(last["sequence"])
