"""Real ledger/actor visibility checks; no mathematical validation or native worker runs."""
from __future__ import annotations

from itertools import count
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import (
    Actor,
    Command,
    Limits,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.runtime.runner import WorkerRunner
from formal_geyser.registry import ObjectRegistry
from formal_geyser.worker_tools import handle_worker_tool
from tests.bootstrap_helpers import synthetic_execution
from tests.coordination.lifecycle_helpers import open_collaboration
from tests.coordination.publication_helpers import file_payload, new_hub

OPERATOR = Actor(role=Role.OPERATOR)
QUERY = "visibilityneedle"
PRIVATE = "private-typed-record-marker"


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}), actor)


def worker(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


@pytest.fixture
async def visibility(tmp_path):
    ticks = count(1000)
    hub = await new_hub(tmp_path,
                       config=ProjectConfig(domain="lean", limits=Limits(packet_bytes=512)),
                       clock=lambda: next(ticks))
    synthetic_execution(hub)
    async def task(title, actor=OPERATOR):
        return await command(hub, "task.publish", {"contract": {
            "title": title, "objective": "discoverytoken shared task", "domain": "lean",
            "acceptance_policy": "lean.peer@1"}}, actor)
    async def claim(task_, run):
        if (await hub.task(task_["id"]))["collaboration"].get("recruitment", {}).get("initial_worker_id"):
            await open_collaboration(hub, task_["id"])
        member = await command(hub, "worker.register")
        return await command(hub, "task.claim", {"task_id": task_["id"], "revision": 1,
            "run_id": run["id"], "worker_id": member["id"], "plan": "Check the shared record visibility."})
    try:
        parent = await task("Publisher home")
        run = await command(hub, "run.start")
        publisher = await claim(parent, run)
        host = await task("Independent child team", worker(publisher))
        author = await claim(host, run)
        peer = await claim(host, run)
        foreign_task = await task("Foreign team")
        foreign = await claim(foreign_task, run)
        private_name, other_name = f"{QUERY} {PRIVATE} " + "private " * 800, f"{QUERY} foreign-private"
        private = await command(hub, "record.private", {
            "kind": "tool.lean-development", "data": {"object_type": "definition", "name": private_name}},
            worker(author))
        other_private = await command(hub, "record.private", {
            "kind": "tool.lean-development", "data": {"object_type": "source", "name": other_name}},
            worker(foreign))
        ordinary = await command(hub, "artifact.stage", file_payload(
            hub, foreign, kind="research.note", path="notes/visibility.json",
            data={"name": f"{QUERY} ordinary", "description": "Synthetic access-control source file."}),
            worker(foreign))
        def create_records(c):
            # A stale historical index must not defeat the ownership boundary.
            for item, name in ((private, private_name), (other_private, other_name)):
                c.execute("INSERT INTO search_index VALUES(?,?,?,?)", (item["id"], item["kind"], name, name))
            specifications = [
                ("foreign", "lean.definition", foreign_task, foreign["worker_id"], "candidate"),
                ("unowned", "lean.definition", None, None, "candidate"),
                ("own", "lean.definition", host, author["worker_id"], "candidate"),
                ("producer_only", "lean.definition", None, author["worker_id"], "candidate"),
                ("accepted", "lean.statement", foreign_task, foreign["worker_id"], "accepted"),
                ("shared", "lean.source", foreign_task, foreign["worker_id"], "candidate"),
            ]
            records = {}
            for label, kind, owner_task, producer, status in specifications:
                record = hub._insert_artifact(c, kind, {"name": f"{QUERY} {label}",
                    "description": "Synthetic access-control fixture only."}, task=owner_task, producer=producer)
                if status == "accepted":
                    # Synthetic state for authorization coverage, not proof acceptance.
                    c.execute("UPDATE artifacts SET status='accepted' WHERE id=?", (record["id"],))
                records[label] = record
            # Pending ordinary source files remain subject to the same raw ACL
            # even if a historical discovery index contains their text. This
            # index row cannot confer acceptance or publication.
            c.execute("INSERT INTO search_index VALUES(?,?,?,?)", (
                ordinary["id"], ordinary["kind"], ordinary["data"]["name"],
                ordinary["data"]["description"]))
            records["ordinary"] = ordinary
            return records
        records = await hub.db.call(create_records, write=True)
        yield SimpleNamespace(hub=hub, host=host, foreign_task=foreign_task, private=private,
            other_private=other_private, records=records, author=author, peer=peer,
            publisher=publisher, foreign=foreign)
    finally:
        await hub.close()


async def test_direct_project_reads_include_notes_and_unrelated_candidates(visibility):
    case, hub = visibility, visibility.hub
    for attempt in (case.author, case.peer, case.publisher, case.foreign):
        for record in (case.private, case.other_private, *case.records.values()):
            assert (await hub.native_record(record["id"], worker(attempt)))["id"] == record["id"]
    assert (await hub.native_record(case.records["unowned"]["id"], OPERATOR))["id"] == case.records["unowned"]["id"]


async def test_project_registry_paginates_all_lean_records_without_exporting_notes(visibility):
    case, hub = visibility, visibility.hub
    expected = {record["id"] for record in case.records.values() if record["kind"].startswith("lean.")}
    observed = []
    for offset in range(len(expected)):
        page = await handle_worker_tool(hub, "lean_registry_list", {"offset": offset, "limit": 1},
                                       worker(case.author), key=f"page-{offset}")
        assert page["total"] == len(expected) and len(page["items"]) == 1
        assert page["next_offset"] == (offset + 1 if offset + 1 < len(expected) else None)
        observed.append(page["items"][0]["id"])
        assert PRIVATE not in repr(page)
    assert set(observed) == expected
    for attempt in (case.peer, case.publisher, case.foreign):
        page = await handle_worker_tool(hub, "lean_registry_list", {}, worker(attempt), key=uuid4().hex)
        assert {item["id"] for item in page["items"]} == expected
    readable = await hub.list_visible_records(worker(case.author), limit=100)
    assert {case.private["id"], case.other_private["id"]} <= {item["id"] for item in readable["items"]}
    global_ids = {item["id"] for item in await ObjectRegistry(hub).list()}
    assert {case.private["id"], case.other_private["id"]}.isdisjoint(global_ids)


async def test_project_search_keeps_private_notes_out_of_shared_discovery(visibility):
    case, hub = visibility, visibility.hub
    first = await hub.search(QUERY, limit=1, actor=worker(case.author))
    assert len(first) == 1 and first[0]["object_id"] in {r["id"] for r in case.records.values()}
    matches = await hub.search(QUERY, limit=100, actor=worker(case.author))
    assert {item["object_id"] for item in matches} == {r["id"] for r in case.records.values()}
    assert PRIVATE not in repr(matches)
    tasks = await hub.search("discoverytoken", limit=100, actor=worker(case.author))
    assert case.foreign_task["id"] in {item["object_id"] for item in tasks}
    runner = WorkerRunner(hub)
    delivered = await runner._dispatch_tool(case.author, "synthetic-thread", "taskhub_call",
        {"operation": "search", "arguments": {"query": QUERY, "limit": 1}}, "actor-forwarding", packet_bytes=4096)
    assert delivered["matches"] == first


async def test_corrupt_project_record_is_reported_without_blocking_other_list_items(visibility):
    case, hub = visibility, visibility.hub
    for record in (case.records["foreign"], case.other_private):
        path = Path(record["content_file"]["path"])
        path.chmod(0o600)
        path.write_bytes(b"corrupted content")
        with pytest.raises(HubError) as error:
            await hub.native_record(record["id"], worker(case.author))
        assert error.value.code == "CONTENT_CHANGED"
    page = await hub.list_visible_records(worker(case.author), kind_prefix="lean.", exclude_private=True)
    assert page["total"] == 6 and len(page["items"]) == 5
    assert page["unavailable"] == [{"id": case.records["foreign"]["id"], "code": "CONTENT_CHANGED"}]
    assert len(await hub.search(QUERY, limit=100, actor=worker(case.author))) == 7
    with pytest.raises(HubError) as operator:
        await hub.native_record(case.records["foreign"]["id"], OPERATOR)
    assert operator.value.code == "CONTENT_CHANGED"


async def test_all_actor_aware_reads_enforce_current_fence_even_for_public_content(visibility):
    case, hub = visibility, visibility.hub
    actor = worker(case.author)
    stale = actor.model_copy(update={"fencing_token": actor.fencing_token + 1})
    calls = [lambda: hub.native_record(case.records["accepted"]["id"], stale),
             lambda: hub.list_visible_records(stale), lambda: hub.search("no-matches", actor=stale)]
    for call in calls:
        with pytest.raises(HubError) as error:
            await call()
        assert error.value.code == "STALE_BINDING"


async def test_oversized_registry_and_search_results_never_copy_private_text_to_shared_blobs(visibility):
    case, hub = visibility, visibility.hub
    runner = WorkerRunner(hub)
    for operation, args in (("lean_registry_list", {}), ("search", {"query": QUERY, "limit": 20})):
        result = await runner._tool(case.author, "synthetic-thread", "taskhub_call",
            {"operation": operation, "arguments": args}, uuid4().hex)
        assert "material_sha256" in result
        assert PRIVATE.encode() not in (hub.workspace(case.author["worker_id"]) / result["material_path"]).read_bytes()
    for path in hub.blobs.root.glob("*/*"):
        if path.is_file():
            assert PRIVATE.encode() not in path.read_bytes()
