"""Decisions request only JSON Schema that the model API accepts for structured output."""
from __future__ import annotations

from uuid import uuid4

import pytest

from formal_geyser.coordination.models import Actor, Command, Role
from formal_geyser.coordination.runtime import (
    ScriptedHarness,
    ScriptedTurn,
    WorkerRunner,
)
from formal_geyser.coordination.runtime.helpers import (
    ASSIGNMENT_SCHEMA,
    RECRUITMENT_SCHEMA,
    decision_schema,
)
from formal_geyser.coordination.storage import canonical
from tests.coordination.publication_helpers import new_hub

# Anything else (allOf, if/then/else, string or number bounds) was rejected with
# invalid_json_schema, so every decision failed before the model ran.
ACCEPTED = {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}


def keywords(schema):
    found = set(schema)
    for value in schema.get("properties", {}).values():
        found |= keywords(value)
    if isinstance(schema.get("items"), dict):
        found |= keywords(schema["items"])
    return found


@pytest.mark.parametrize("schema", [ASSIGNMENT_SCHEMA, RECRUITMENT_SCHEMA])
def test_requested_output_schemas_use_only_accepted_keywords(schema):
    assert keywords(schema) <= ACCEPTED
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


async def test_assignment_decision_requests_a_schema_of_the_offered_names(tmp_path):
    hub = await new_hub(tmp_path / "project")

    async def command(name, payload=None):
        return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex), Actor(role=Role.OPERATOR))
    try:
        run = await command("run.start")
        await command("worker.register")  # Idle: it has finished (here: never had) a task.
        await command("task.publish", {"contract": {"title": "Pool task", "objective": "Inspect the pool."}})
        assert await hub.next_assignment(run["id"]) is None
        [intent] = await hub.due_assignment_intents(run["id"])

        class Recording(ScriptedHarness):
            def set_turn_options(self, *, output_schema=None, client_input_id=None):
                self.output_schema = output_schema

        harness = Recording([ScriptedTurn([], visible_output=canonical({"worker": None}))])
        result = await WorkerRunner(hub, lambda: harness).run_helper(intent)
        assert harness.output_schema == decision_schema(intent)
        assert harness.output_schema["properties"]["worker"]["enum"][-1] is None
        assert {key: value for key, value in harness.output_schema.items() if key != "properties"} == {
            key: value for key, value in ASSIGNMENT_SCHEMA.items() if key != "properties"}
        assert result["outcome"] == "new_worker"
    finally:
        await hub.close()
