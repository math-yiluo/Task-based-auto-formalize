"""Read-only, paged task views sharing the scheduler's pool eligibility.

Summary queries never create a run, claim work, acknowledge messages, or alter
historical task contracts. Large contracts and review histories are fetched only
when opening a detail, not on every overview poll.
"""
from __future__ import annotations

import json
from typing import Any

from .errors import HubError
from .worker_names import worker_identity

VIEWS = frozenset({"all", "running", "pool", "history"})
TERMINAL = frozenset({"done", "retired"})
_TASK_ROWS = """
SELECT t.id,t.current_revision,t.state,t.state_version,t.created_at,
       r.task_id,r.revision,r.contract_hash,r.contract_json
FROM tasks t JOIN task_revisions r ON r.task_id=t.id AND r.revision=t.current_revision
"""


def _pagination(offset: int, limit: int) -> None:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise HubError("INVALID_INPUT", "Use a nonnegative offset and a page size from 1 to 100", 422)


def _page_offset(offset: int, limit: int, total: int) -> int:
    return min(offset, ((total - 1) // limit) * limit) if total else 0


def _running_ids(c) -> set[str]:
    # A correspondence wake retains a task pointer without resuming its work.
    # Use both persisted modes: ordinary message wakes are recorded on workers,
    # whereas taskless correspondence attempts also carry message_only.
    return {row[0] for row in c.execute(
        "SELECT DISTINCT a.task_id FROM attempts a JOIN workers w ON w.id=a.worker_id "
        "JOIN tasks t ON t.id=a.task_id WHERE a.ended_at IS NULL "
        "AND a.state IN ('running','draining') AND w.active_attempt=a.id "
        "AND a.execution_mode='work' AND w.execution_mode='work' "
        "AND w.state<>'recovery_required' "
        "AND (t.state NOT IN ('done','retired') OR a.state='draining')")}


def _member_status(member: dict, task_id: str, attempts: dict, terminal: bool) -> str:
    active = attempts.get(member["worker_id"])
    # A departed or closed member may still be safely draining its last process.
    if active and active["state"] == "draining":
        return "Finishing turn"
    if member["membership_state"] == "left":
        return "Left"
    if terminal or member["membership_state"] == "ended":
        return "Ended"
    if active:
        if active["state"] == "recovery_required" or member["worker_state"] == "recovery_required":
            return "Recovering"
        if member["execution_mode"] != "work" or active["execution_mode"] != "work":
            return "Handling message"
        if active["state"] == "running":
            return "Working"
    if member["worker_state"] == "recovery_required" and member["current_task_id"] == task_id:
        return "Recovering"
    if member["current_task_id"] != task_id:
        return ("Working elsewhere" if member["active_attempt"]
                and member["worker_state"] in {"running", "draining"}
                and member["execution_mode"] == "work" else "Retained")
    if member["work_intention"] == "waiting" or member["worker_state"] == "suspended":
        return "Waiting"
    if member["work_intention"] == "working" and member["runnable"]:
        return "Ready"
    return "Retained"


def _executions(c, task_ids: list[str], terminal: set[str]) -> dict[str, tuple[list[dict], list[dict]]]:
    """Each task's members, with their execution status, and its live attempts, read together
    (`terminal`: the tasks that are done or retired)."""
    ids = json.dumps(task_ids)
    attempts: dict[str, dict[str, dict]] = {task_id: {} for task_id in task_ids}
    for item in c.execute(
            "SELECT id,worker_id,task_id,state,execution_mode,created_at,ended_at FROM attempts "
            "WHERE task_id IN (SELECT value FROM json_each(?)) AND ended_at IS NULL ORDER BY created_at,id", (ids,)):
        attempts[item["task_id"]][item["worker_id"]] = dict(item)
    members: dict[str, list[dict]] = {task_id: [] for task_id in task_ids}
    for item in c.execute(
            "SELECT m.task_id,m.worker_id,m.executor,m.runnable,m.work_reason,m.membership_state,"
            "w.worker_number,w.reviewer_number,w.kind,w.state AS worker_state,w.current_task_id,w.active_attempt,"
            "w.work_intention,w.execution_mode FROM task_members m JOIN workers w ON w.id=m.worker_id "
            "WHERE m.task_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY m.joined_at,w.worker_number,w.reviewer_number", (ids,)):
        task_id = item["task_id"]
        member = worker_identity(dict(item))
        entry = {key: member[key] for key in (
            "worker_id", "executor", "runnable", "work_reason", "membership_state", "worker_number", "reviewer_number", "kind")}
        entry.update(display_name=member.get("display_name", member["worker_id"]),
                     execution_status=_member_status(member, task_id, attempts[task_id], task_id in terminal))
        members[task_id].append(entry)
    return {task_id: (members[task_id], list(attempts[task_id].values())) for task_id in task_ids}


def _summaries(c, rows, *, running: set[str], opportunities: dict | None = None) -> list[dict[str, Any]]:
    """The summaries of these task rows, read together: the same few statements for any number
    of tasks. The dashboard's overview summarizes every task a worker refers to (573 in the
    pilot) every two seconds; task by task that was 4,519 statements, 22 to 31 s on the busy
    service."""
    task_ids = [row["id"] for row in rows]
    ids = json.dumps(task_ids)
    executions = _executions(c, task_ids, {row["id"] for row in rows if row["state"] in TERMINAL})

    def by_task(sql):
        return {row[0]: row for row in c.execute(sql, (ids,))}
    lifecycles = by_task("SELECT task_id,opportunity_state,recruitment_decision,invitation FROM task_lifecycle "
                         "WHERE task_id IN (SELECT value FROM json_each(?))")
    # The list needs a verdict, not every receipt, rationale or integration (one receipt a review).
    reviews = by_task("SELECT r.task_id,r.state,r.candidate_id,p.verdict,p.qualifications_json FROM review_requests r "
                      "LEFT JOIN peer_review_receipts p ON p.review_task_id=r.task_id "
                      "WHERE r.task_id IN (SELECT value FROM json_each(?))")
    pending = {row[0]: row[1] for row in c.execute(
        "SELECT host_task_id,COUNT(*) FROM review_requests WHERE host_task_id IN (SELECT value FROM json_each(?)) "
        "AND state IN ('pending','integrating') GROUP BY host_task_id", (ids,))}
    invalid = {(row[0], row[1]) for row in c.execute(
        "SELECT DISTINCT task_id,task_revision FROM completion_evidence_invalidations "
        "WHERE task_id IN (SELECT value FROM json_each(?))", (ids,))}
    repairs = by_task("SELECT repair_task_id,task_id,task_revision FROM completion_evidence_invalidations "
                      "WHERE repair_task_id IN (SELECT value FROM json_each(?))")
    placed = by_task("SELECT task_id,priority,pinned_at FROM task_admission WHERE task_id IN (SELECT value FROM json_each(?))")
    summaries = []
    for row in rows:
        # A summary shows only the title and kind, so it reads the ledger's copy of the
        # contract; the task files are read and verified when a task is opened or executed.
        contract = json.loads(row["contract_json"])
        task_id = row["id"]
        members, attempts = executions[task_id]
        collaboration: dict[str, Any] = {"members": members}
        lifecycle = lifecycles.get(task_id)
        if lifecycle:
            collaboration["recruitment"] = {key: lifecycle[key] for key in (
                "opportunity_state", "recruitment_decision", "invitation")}
        review = reviews.get(task_id)
        if review:
            value: dict[str, Any] = {"state": review["state"], "candidate_id": review["candidate_id"]}
            if review["verdict"] is not None:
                value["feedback"] = {"verdict": review["verdict"],
                                     "qualifications": json.loads(review["qualifications_json"])}
            value["acceptance"] = review["state"] == "integrating"
            collaboration["review"] = value
        opportunity = (opportunities or {}).get(task_id)
        result = {key: row[key] for key in ("id", "state", "revision", "current_revision", "created_at", "state_version")}
        # The operator's place for it in admission, if any (task.set_priority, task.set_pinned).
        place, contract_priority = placed.get(task_id), contract.get("priority", 1)
        operator_priority = place["priority"] if place else None
        result["admission"] = {
            "priority": contract_priority if operator_priority is None else operator_priority,
            "contract_priority": contract_priority, "operator_priority": operator_priority,
            "pinned_at": place["pinned_at"] if place else None}
        result.update(contract={"title": contract["title"], "kind": contract["kind"]},
                      collaboration=collaboration, active_attempts=attempts, running=task_id in running,
                      reviews_pending=pending.get(task_id, 0),
                      pool_opportunity=({key: opportunity[key] for key in (
                          "opportunity", "invitation", "opened_at", "executors")} if opportunity else None))
        if (task_id, row["revision"]) in invalid:
            result["current_evidence"] = {"state": "invalid"}
        repair = repairs.get(task_id)
        if repair:
            result["repair_of"] = {"task_id": repair["task_id"], "task_revision": repair["task_revision"]}
        summaries.append(result)
    return summaries


async def task_page(hub, *, view: str = "all", query: str = "", offset: int = 0,
                    limit: int = 25) -> dict:
    _pagination(offset, limit)
    if view not in VIEWS or not isinstance(query, str) or len(query) > 1000:
        raise HubError("INVALID_INPUT", "Choose a task view and a search query of at most 1000 characters", 422)
    query = query.strip()

    def read(c):
        running = _running_ids(c)
        # This is the exact scheduler helper, not a frontend approximation such
        # as state == ready. An executing task can offer a collaborator slot.
        pool = {item["task_id"]: item for item in hub._opportunities(c)}
        counts = {"all": c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                  "running": len(running), "pool": len(pool),
                  "history": c.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('done','retired')").fetchone()[0]}
        conditions, values = [], []
        if view in {"running", "pool"}:
            ids = running if view == "running" else set(pool)
            # JSON avoids SQLite's placeholder limit for large project pools.
            conditions.append("t.id IN (SELECT value FROM json_each(?))")
            values.append(json.dumps(sorted(ids)))
        elif view == "history":
            conditions.append("t.state IN ('done','retired')")
        if query:
            conditions.append("instr(lower(t.id || ' ' || t.state || ' ' || "
                              "json_extract(r.contract_json,'$.title') || ' ' || "
                              "json_extract(r.contract_json,'$.kind')),lower(?))>0")
            values.append(query)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        # The immutable contract cache is a search index only. Displayed bodies
        # are always checked against their authoritative native file when a task is opened.
        if view == "pool":
            # In the order TaskHub takes them: reviews, then pinned tasks, priority and age.
            matching = {row[0] for row in c.execute("SELECT t.id FROM (" + _TASK_ROWS + where + ") t", values)}
            ordered = [item["task_id"] for item in sorted(
                pool.values(), key=lambda item: (item["opportunity"] != "review", hub._admission_key(item)))
                if item["task_id"] in matching]
            total = len(ordered)
            start = _page_offset(offset, limit, total)
            shown = ordered[start:start + limit]
            found = {row["id"]: row for row in c.execute(
                _TASK_ROWS + " WHERE t.id IN (SELECT value FROM json_each(?))", (json.dumps(shown),))}
            rows = [found[task_id] for task_id in shown if task_id in found]
        else:
            total = c.execute("SELECT COUNT(*) FROM (" + _TASK_ROWS + where + ")", values).fetchone()[0]
            start = _page_offset(offset, limit, total)
            rows = c.execute(_TASK_ROWS + where + " ORDER BY t.created_at DESC,t.id DESC LIMIT ? OFFSET ?",
                             (*values, limit, start)).fetchall()
        return {"view": view, "query": query, "items": _summaries(c, rows, running=running, opportunities=pool),
                "total": total, "counts": counts, "offset": start, "limit": limit,
                "has_more": start + len(rows) < total,
                "next_offset": start + limit if start + len(rows) < total else None}
    return await hub.db.call(read)


async def task_revision_page(hub, task_id: str, *, offset: int = 0, limit: int = 20) -> dict:
    _pagination(offset, limit)

    def read(c):
        task = hub._get(c, "tasks", task_id)
        total = c.execute("SELECT COUNT(*) FROM task_revisions WHERE task_id=?", (task_id,)).fetchone()[0]
        start = _page_offset(offset, limit, total)
        rows = c.execute("SELECT * FROM task_revisions WHERE task_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",
                         (task_id, limit, start)).fetchall()
        return {"items": [{"revision": row["revision"], "contract_hash": row["contract_hash"],
                           "title": hub._task_contract(c, row)["title"]} for row in rows],
                "task_id": task_id, "current_revision": task["current_revision"], "total": total,
                "offset": start, "limit": limit, "has_more": start + len(rows) < total,
                "next_offset": start + limit if start + len(rows) < total else None}
    return await hub.db.call(read)


async def task_detail(hub, task_id: str, *, revision: int | None = None) -> dict:
    """Keep the selected contract exact while labeling current live execution."""
    def read(c):
        result = hub._task(c, task_id, revision=revision)
        members, _ = _executions(c, [task_id], {task_id} if result["state"] in TERMINAL else set())[task_id]
        statuses = {member["worker_id"]: member["execution_status"] for member in members}
        for member in result["collaboration"]["members"]:
            member["execution_status"] = statuses.get(member["worker_id"], "Retained")
        result["running"] = task_id in _running_ids(c)
        if revision is None:
            result["admission"] = hub._admission(c, result)
        return result
    return await hub.db.call(read)


async def task_overview(hub) -> dict:
    """Only the task labels workers not retired refer to are needed outside the task browser:
    a retired reviewer keeps its review as its current task, so all of them were 3,700 tasks,
    half of a 7 MB overview, in the Linux pilot."""
    def read(c):
        running = _running_ids(c)
        rows = c.execute(_TASK_ROWS + " WHERE t.id IN ("
            "SELECT current_task_id FROM workers WHERE current_task_id IS NOT NULL AND state<>'retired' "
            "UNION SELECT m.task_id FROM task_members m JOIN tasks retained ON retained.id=m.task_id "
            "WHERE m.executor=1 AND m.membership_state='member' AND retained.state NOT IN ('done','retired')) "
            "ORDER BY t.created_at,t.id").fetchall()
        return {"tasks": _summaries(c, rows, running=running),
                "task_count": c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]}
    # SQL only: the dashboard must not wait behind the coordination thread.
    return await hub.db.read(read)


async def task_outline(hub) -> dict:
    """Every task once, for progress at a glance; reviews name the task they review."""
    def read(c):
        running = _running_ids(c)
        hosts = {row["task_id"]: row["host_task_id"] for row in c.execute(
            "SELECT task_id,host_task_id FROM task_collaboration WHERE host_task_id IS NOT NULL")}
        root = c.execute("SELECT root_task_id FROM project_lifecycle WHERE id=1").fetchone()
        items = _summaries(c, c.execute(_TASK_ROWS + " ORDER BY t.created_at,t.id").fetchall(), running=running)
        for item in items:
            item["host_task_id"] = hosts.get(item["id"])
        return {"root_task_id": root["root_task_id"] if root else None, "items": items}
    return await hub.db.read(read)
