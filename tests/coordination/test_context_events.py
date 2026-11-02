"""Producer-side event selection without lost notifications or audit evidence."""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.context_events import routine_for_recipient
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.storage import canonical

DONE = {"status": "completed", "terminal_observed": True, "process_quiet": True}
CLAIM = {"attempt_id": "attempt", "fencing_token": 1}


@pytest.mark.parametrize("kind,payload,owner,routine", [
    ("turn.finished", DONE, "self", True),
    ("task.claimed", CLAIM, "self", True),
    ("task.claimed", CLAIM, "peer", False),
    ("turn.finished", DONE, "peer", False),
    ("turn.finished", {**DONE, "status": "failed"}, "self", False),
    ("turn.finished", {**DONE, "status": "interrupted"}, "self", False),
    ("turn.finished", {**DONE, "status": "unknown"}, "self", False),
    ("turn.finished", {**DONE, "process_quiet": False}, "self", False),
    ("turn.finished", {**DONE, "terminal_observed": 1}, "self", False),
    ("turn.finished", {"status": "completed"}, "self", False),
    ("turn.finished", {**DONE, "new_fact": "must be seen"}, "self", False),
    ("task.claimed", {**CLAIM, "fencing_token": True}, "self", False),
    ("task.claimed", {**CLAIM, "attempt_id": ""}, "self", False),
    ("task.claimed", {**CLAIM, "scope_changed": True}, "self", False),
    ("task.claimed", None, "self", False),
    ("future.notice", DONE, "self", False),
])
def test_only_exact_successful_self_bookkeeping_is_routine(kind, payload, owner, routine):
    assert routine_for_recipient(kind, payload, owner, "self") is routine


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, key=uuid4().hex, payload=payload or {}),
                             actor or Actor(role=Role.OPERATOR))


async def ack(case, packet):
    return await command(case.hub, "context.ack", {
        "worker_id": case.actor.worker_id, "packet_id": packet["packet_id"]}, case.actor)


@pytest.fixture
async def project(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    task = await command(hub, "task.publish", {"contract": {"title": "Event delivery fixture",
        "objective": "Retain exact synthetic notifications.", "acceptance_policy": "general.peer@1"}})
    run = await command(hub, "run.start")
    attempt = await hub.next_assignment(run["id"])
    actor = Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                  fencing_token=attempt["fencing_token"])
    case = SimpleNamespace(hub=hub, task=task, run=run, actor=actor, attempt=attempt)
    await ack(case, await hub.context(actor.worker_id))
    try:
        yield case
    finally:
        await hub.close()


async def append(case, records):
    return await case.hub.db.call(lambda c: [case.hub._event(
        c, kind, str(index), payload, case.task["id"], owner or case.actor.worker_id)
        for index, (kind, payload, owner) in enumerate(records)], write=True)


async def event_rows(hub):
    return await hub.db.call(lambda c: [dict(row) for row in c.execute("SELECT * FROM events ORDER BY sequence")])


async def test_routine_only_packet_replays_until_ack_and_leaves_raw_ledger_unchanged(project):
    case = project
    ids = await append(case, [("turn.finished", DONE, None), ("task.claimed", CLAIM, None)] * 300)
    before = await event_rows(case.hub)
    first = await case.hub.context(case.actor.worker_id)
    repeat = await case.hub.context(case.actor.worker_id)
    assert first["updates"] == repeat["updates"] == []
    assert first["through_sequence"] == repeat["through_sequence"] == ids[-1]
    assert first["after_sequence"] == repeat["after_sequence"] < ids[0]
    assert not first["requires_sync"] and not first["has_more_updates"]
    assert await event_rows(case.hub) == before
    await ack(case, first)
    after = await case.hub.context(case.actor.worker_id)
    assert after["after_sequence"] == ids[-1] and after["updates"] == []
    # A delayed receipt cannot regress the MAX delivered cursor.
    await ack(case, repeat)
    assert (await case.hub.context(case.actor.worker_id))["after_sequence"] == ids[-1]


async def test_long_routine_prefix_does_not_hide_business_failure_peer_or_unknown_events(project):
    case = project
    records = [("turn.finished", DONE, None)] * 500
    wanted = [("task.revise", {"revision": 2}, None),
              ("review.requested", {"candidate_id": "candidate"}, None),
              ("turn.finished", {**DONE, "status": "failed"}, None),
              ("task.claimed", CLAIM, "another-worker"),
              ("new.domain.notice", {"message": "Exact new domain content"}, None)]
    records.extend(wanted)
    records.extend([("turn.finished", DONE, None)] * 300)
    ids = await append(case, records)
    packet = await case.hub.context(case.actor.worker_id)
    assert [(item["kind"], item["payload"]) for item in packet["updates"]] == [(kind, payload) for kind, payload, _ in wanted]
    assert packet["through_sequence"] == ids[-1]
    assert not packet["has_more_updates"]
    replay = await case.hub.context(case.actor.worker_id)
    assert replay["updates"] == packet["updates"]


@pytest.mark.parametrize("pending", [False, True])
async def test_visible_count_limit_scans_routine_tail_without_crossing_unsent_event(project, pending):
    case = project
    # Give the count limit, rather than the byte limit, control of this fixture.
    await case.hub.db.call(lambda c: c.execute("UPDATE runs SET limits_json=? WHERE id=?", (
        canonical({**json.loads(c.execute("SELECT limits_json FROM runs WHERE id=?", (case.run["id"],)).fetchone()[0]),
                   "packet_bytes": 262144, "delta_bytes": 131072}), case.run["id"])), write=True)
    records = [("domain.fact", {"number": number}, None) for number in range(200)]
    records.extend([("turn.finished", DONE, None)] * 300)
    if pending:
        records += [("dependency.changed", {"version": 7}, None), ("turn.finished", DONE, None)]
    ids = await append(case, records)
    packet = await case.hub.context(case.actor.worker_id)
    assert len(packet["updates"]) == 200
    assert packet["through_sequence"] == ids[499]
    assert packet["has_more_updates"] is pending and packet["requires_sync"] is pending
    await ack(case, packet)
    next_packet = await case.hub.context(case.actor.worker_id)
    assert [item["sequence"] for item in next_packet["updates"]] == ([ids[500]] if pending else [])
    assert next_packet["through_sequence"] == ids[-1]


async def test_byte_limited_pages_preserve_every_visible_event_and_do_not_ack_unsent_gaps(project):
    case = project
    await case.hub.db.call(lambda c: c.execute("UPDATE runs SET limits_json=? WHERE id=?", (
        canonical({**json.loads(c.execute("SELECT limits_json FROM runs WHERE id=?", (case.run["id"],)).fetchone()[0]),
                   "delta_bytes": 1024}), case.run["id"])), write=True)
    records = []
    for number in range(12):
        records += [("turn.finished", DONE, None), ("domain.fact", {"text": str(number) + " exact content" * 10}, None)]
    ids = await append(case, records)
    expected, received = ids[1::2], []
    for _ in range(20):
        packet = await case.hub.context(case.actor.worker_id)
        replay = await case.hub.context(case.actor.worker_id)
        assert replay["updates"] == packet["updates"]
        received.extend(item["sequence"] for item in packet["updates"])
        remaining = [value for value in expected if value not in received]
        if remaining:
            assert packet["through_sequence"] < remaining[0]
            assert packet["requires_sync"]
        await ack(case, packet)
        if not packet["has_more_updates"]:
            break
    assert received == expected


async def test_unfit_visible_event_still_fails_after_routine_prefix_without_new_receipt(project):
    case = project
    await append(case, [("turn.finished", DONE, None), ("domain." + "x" * 40000, {"important": True}, None)])
    before = await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM deliveries").fetchone()[0])
    with pytest.raises(HubError, match="next update cannot fit"):
        await case.hub.context(case.actor.worker_id)
    assert await case.hub.db.call(lambda c: c.execute("SELECT count(*) FROM deliveries").fetchone()[0]) == before


@pytest.mark.parametrize("long_invitation", [False, True])
async def test_recruitment_projection_removes_only_correlation_ids_from_inline_and_material_views(project, long_invitation):
    case = project
    invitation = "Exact contribution plan. " * (180 if long_invitation else 1)
    await case.hub.db.call(lambda c: c.execute(
        "UPDATE task_lifecycle SET recruitment_decision='yes',opportunity_state='open',invitation=?,"
        "first_turn_id='private-turn-correlation',decision_helper_id='private-helper-correlation' WHERE task_id=?",
        (invitation, case.task["id"])), write=True)
    source = (await case.hub.task(case.task["id"]))["collaboration"]
    packet = await case.hub.context(case.actor.worker_id)
    view = packet["task"]["collaboration"]
    if long_invitation:
        assert view["requires_read"]
        view = json.loads((case.hub.workspace(case.actor.worker_id) / view["material_path"]).read_text(encoding="utf-8"))
    else:
        assert "requires_read" not in view
    assert view["recruitment"] == {key: value for key, value in source["recruitment"].items()
                                   if key not in {"first_turn_id", "decision_helper_id"}}
    assert view["recruitment"]["invitation"] == invitation
    assert (await case.hub.task(case.task["id"]))["collaboration"] == source


async def test_receipt_prepared_before_filtering_remains_acknowledgeable(project):
    case = project
    ids = await append(case, [("turn.finished", DONE, None)])
    old_id = "historical-packet"
    body = {"updates": [{"sequence": ids[0], "kind": "turn.finished", "payload": DONE}]}
    sha = case.hub.blobs.put_json(body)
    epoch = (await case.hub.worker(case.actor.worker_id))["context_epoch"]
    await case.hub.db.call(lambda c: c.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,'pending',?)", (
        old_id, case.actor.worker_id, epoch, ids[0], sha, case.hub.clock())), write=True)
    await ack(case, {"packet_id": old_id})
    assert (await case.hub.context(case.actor.worker_id))["after_sequence"] == ids[0]
