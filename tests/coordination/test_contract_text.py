"""A task's objective and context are its text: a contract that gives only a file name, as the
stored task.json does, names no target and is refused on publication and revision."""
from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, Command, Role, file_name_fields
from tests.coordination.publication_helpers import new_hub


async def command(hub, name, payload=None):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), Actor(role=Role.OPERATOR))


def test_file_names_are_told_from_text():
    assert file_name_fields({"objective": "objective.md", "context": " context.md\n"}) == ["objective", "context"]
    assert file_name_fields({"objective": "spec/objective.markdown", "context": ""}) == ["objective"]
    assert file_name_fields({"objective": "Prove the statement in objective.md.", "context": "See README.md"}) == []
    assert file_name_fields({"objective": "# Target\n\nobjective.md"}) == []


@pytest.mark.parametrize("field", ["objective", "context"])
async def test_a_contract_naming_a_file_instead_of_its_text_is_refused(tmp_path, field):
    hub = await new_hub(tmp_path)
    try:
        task = await command(hub, "task.publish", {"contract": {
            "title": "Has its text", "objective": "Prove the stated proposition.", "context": "Background."}})
        named = {"title": "Names a file", "objective": "Prove it.", "context": "Background.",
                 field: field + ".md"}
        with pytest.raises(HubError) as published:
            await command(hub, "task.publish", {"contract": named})
        assert published.value.code == "CONTRACT_TEXT_REQUIRED" and field in published.value.message
        with pytest.raises(HubError) as revised:
            await command(hub, "task.revise", {"task_id": task["id"], "revision": 1, "contract": named,
                                               "reason": "A revision that would empty the task."})
        assert revised.value.code == "CONTRACT_TEXT_REQUIRED"
        assert [item["id"] for item in await hub.list_tasks()] == [task["id"]]
        assert (await hub.task(task["id"]))["revision"] == 1
    finally:
        await hub.close()
