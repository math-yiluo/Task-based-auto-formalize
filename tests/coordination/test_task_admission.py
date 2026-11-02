"""The operator places a task in admission: a priority in place of its contract's, and a pin to
the top. Neither revises the task."""
from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role
from formal_geyser.coordination.run_driver import WAKE_COMMANDS
from formal_geyser.coordination.task_browser import task_detail, task_page

OPERATOR = Actor(role=Role.OPERATOR)


async def command(hub, name, payload=None, actor=OPERATOR):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), actor)


@pytest.fixture
async def hub(tmp_path):
    initialize(tmp_path, ProjectConfig())
    result = Hub(tmp_path)
    now = [1_000_000]

    def clock():  # Each task is published a second after the one before.
        now[0] += 1000
        return now[0]
    result.clock = clock
    try:
        yield result
    finally:
        await result.close()


async def publish(hub, title, priority=1):
    return await command(hub, "task.publish", {"contract": {
        "title": title, "objective": "Produce the " + title + " result.", "priority": priority}})


async def pool(hub):
    """The titles of the open new tasks, in the order admission takes them."""
    return await hub.db.call(lambda c: [item["title"] for item in hub._open_opportunities(c)])


async def test_new_tasks_are_taken_by_priority_then_the_earliest_published(hub):
    # The time a task opened came first, so a task of priority 0 waited behind every task
    # published before it.
    await publish(hub, "early ordinary")
    urgent = await publish(hub, "late urgent", 0)
    await publish(hub, "later urgent", 0)
    assert await pool(hub) == ["late urgent", "later urgent", "early ordinary"]
    run = await command(hub, "run.start")
    assert (await hub.next_assignment(run["id"]))["task_id"] == urgent["id"]


async def test_the_operator_sets_a_priority_and_pins_tasks_without_revising_them(hub):
    first, second, third = [await publish(hub, title) for title in ("first", "second", "third")]
    assert await command(hub, "task.set_priority", {"task_id": third["id"], "priority": 0}) == {
        "task_id": third["id"], "priority": 0, "contract_priority": 1, "operator_priority": 0, "pinned_at": None}
    assert await pool(hub) == ["third", "first", "second"]
    pinned = await command(hub, "task.set_pinned", {"task_id": second["id"], "pinned": True})
    again = await command(hub, "task.set_pinned", {"task_id": second["id"], "pinned": True})
    assert pinned["pinned_at"] is not None and again["pinned_at"] == pinned["pinned_at"]
    await command(hub, "task.set_pinned", {"task_id": first["id"], "pinned": True})
    assert await pool(hub) == ["second", "first", "third"]  # Pinned ones as they were pinned.
    await command(hub, "task.set_pinned", {"task_id": second["id"], "pinned": False})
    await command(hub, "task.set_priority", {"task_id": third["id"], "priority": None})
    assert await pool(hub) == ["first", "second", "third"]
    assert [(await hub.task(task["id"]))["revision"] for task in (first, second, third)] == [1, 1, 1]
    assert [event["kind"] for event in await hub.events(limit=1000) if event["kind"] in {
        "task.priority_set", "task.pinned", "task.unpinned"}] == [
        "task.priority_set", "task.pinned", "task.pinned", "task.pinned", "task.unpinned", "task.priority_set"]
    assert {"task.set_priority", "task.set_pinned"} <= WAKE_COMMANDS  # Admission looks again at once.


async def test_a_pinned_task_goes_before_tasks_of_a_higher_priority(hub):
    await publish(hub, "urgent", 0)
    later = await publish(hub, "later", 3)
    await command(hub, "task.set_pinned", {"task_id": later["id"], "pinned": True})
    run = await command(hub, "run.start")
    assert (await hub.next_assignment(run["id"]))["task_id"] == later["id"]
    # Saved work and reviews are listed with pins first as well.
    assert await hub.db.call(lambda c: [hub._admission_candidates(c, reviews=reviews, pinned=pinned)
                                        for reviews, pinned in ((False, True), (False, False), (True, None))]) == [[], [], []]


async def test_only_the_operator_places_an_unfinished_task(hub):
    task = await publish(hub, "placed")
    for name, payload in (("task.set_priority", {"priority": 4}), ("task.set_priority", {"priority": "0"}),
                          ("task.set_priority", {"priority": True}), ("task.set_priority", {}),
                          ("task.set_pinned", {"pinned": 1}), ("task.set_pinned", {})):
        with pytest.raises(HubError) as error:
            await command(hub, name, {"task_id": task["id"], **payload})
        assert error.value.code == "INVALID_INPUT"
    for name, payload in (("task.set_priority", {"priority": 0}), ("task.set_pinned", {"pinned": True})):
        with pytest.raises(HubError) as error:
            await command(hub, name, {"task_id": task["id"], **payload}, Actor(role=Role.RUNNER))
        assert error.value.status == 403
    await command(hub, "task.retire", {"task_id": task["id"], "revision": 1, "reason": "No longer needed."})
    with pytest.raises(HubError) as error:
        await command(hub, "task.set_pinned", {"task_id": task["id"], "pinned": True})
    assert error.value.code == "TASK_FINISHED"


async def test_the_task_pool_lists_tasks_in_admission_order_with_their_place(hub):
    ordinary, urgent = await publish(hub, "ordinary"), await publish(hub, "urgent", 0)
    page = await task_page(hub, view="pool")
    assert [item["id"] for item in page["items"]] == [urgent["id"], ordinary["id"]]
    await command(hub, "task.set_pinned", {"task_id": ordinary["id"], "pinned": True})
    page = await task_page(hub, view="pool")
    assert [item["id"] for item in page["items"]] == [ordinary["id"], urgent["id"]]
    assert page["items"][0]["admission"]["pinned_at"] is not None
    assert page["items"][1]["admission"] == {
        "priority": 0, "contract_priority": 0, "operator_priority": None, "pinned_at": None}
    assert [item["id"] for item in (await task_page(hub, view="pool", query="urgent"))["items"]] == [urgent["id"]]
    assert (await task_page(hub, view="pool", limit=1, offset=1))["items"][0]["id"] == urgent["id"]
    assert (await task_detail(hub, ordinary["id"]))["admission"]["pinned_at"] is not None
