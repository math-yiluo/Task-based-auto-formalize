"""Never turn a pre-inference transport failure into a recruitment decision."""
import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.maintenance import require_quiet_project
from tests.coordination.publication_helpers import new_hub
from tests.coordination.test_collaborative_core import claim, command, publish, start


async def prepared(hub):
    run = await start(hub)
    task = await publish(hub)
    attempt = await claim(hub, task, run)
    await hub.ordinary_turn_completed(attempt["worker_id"], attempt["id"])
    helper = await hub.db.call(lambda c: hub._helper(c, c.execute(
        "SELECT decision_helper_id FROM task_lifecycle WHERE task_id=?", (task["id"],)).fetchone()[0]))
    await hub.helper_uncertain(helper["id"], "Decision reply transport failed", inference_uncertain=False)
    return run, task, helper


async def test_preflight_retry_preserves_pending_recruitment_and_stays_paused(tmp_path):
    hub = await new_hub(tmp_path)
    try:
        run, task, helper = await prepared(hub)
        await command(hub, "run.pause", {"run_id": run["id"]})
        recovered = await command(hub, "helper.reconcile", {"intent_id": helper["id"],
            "retry_preflight": True, "process_confirmed_stopped": True, "reason": "No inference was dispatched; owned process tree is quiet."})
        assert recovered["state"] == "prepared"
        lifecycle = await hub.db.call(lambda c: dict(c.execute("SELECT * FROM task_lifecycle WHERE task_id=?", (task["id"],)).fetchone()))
        assert lifecycle["recruitment_decision"] == "pending"
        assert lifecycle["decision_helper_id"] == helper["id"]
        assert (await hub.run(run["id"]))["state"] == "paused"
        require_quiet_project(tmp_path)
        assert await hub.due_recruitment_intents(run["id"]) == []
    finally:
        await hub.close()


@pytest.mark.parametrize("field,value", [("inference_uncertain", 1), ("thread_id", "bound"),
    ("remote_turn_id", "sent"), ("raw_result_json", "{}"), ("usage_json", "{}")])
async def test_preflight_retry_refuses_unknown_or_already_started_work(tmp_path, field, value):
    hub = await new_hub(tmp_path)
    try:
        _, _, helper = await prepared(hub)
        await hub.db.call(lambda c: c.execute(f"UPDATE helper_intents SET {field}=? WHERE id=?", (value, helper["id"])), write=True)
        with pytest.raises(HubError) as failure:
            await command(hub, "helper.reconcile", {"intent_id": helper["id"],
                "retry_preflight": True, "process_confirmed_stopped": True, "reason": "Insufficient no-inference evidence."})
        assert failure.value.code == "PREFLIGHT_NOT_SAFE"
    finally:
        await hub.close()
