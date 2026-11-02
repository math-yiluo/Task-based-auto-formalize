"""Empty live inbox polls stay cheap without weakening actual delivery."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.models import Actor, CodexConfig, Command, Role
from formal_geyser.coordination.runtime import (
    CodexHarness,
    ScriptedHarness,
    SteerOutcome,
    TurnOutcome,
    WorkerRunner,
)
from formal_geyser.coordination.runtime import runner as runner_module
from formal_geyser.coordination.transcripts import TranscriptStore
from tests.coordination.lifecycle_helpers import open_collaboration
from tests.coordination.publication_helpers import new_hub


async def command(hub, name, payload=None, actor=None):
    return await hub.execute(Command(name=name, payload=payload or {}, key=uuid4().hex),
                             actor or Actor(role=Role.OPERATOR))


def actor(attempt):
    return Actor(role=Role.WORKER, worker_id=attempt["worker_id"], attempt_id=attempt["id"],
                 fencing_token=attempt["fencing_token"])


@pytest.fixture
async def project(tmp_path):
    hub = await new_hub(tmp_path)
    task = await command(hub, "task.publish", {"contract": {
        "title": "Observe delivery", "objective": "Retain one exact message."}})
    run = await command(hub, "run.start")
    first = await hub.next_assignment(run["id"])
    try:
        yield hub, task, run, first
    finally:
        await hub.close()


@pytest.mark.parametrize("kind", ["human", "peer"])
async def test_signal_preserves_delivery_holds_and_acknowledgment(project, kind):
    hub, task, run, first = project
    worker_id = first["worker_id"]
    assert not await hub.pending_message_signal(worker_id)
    assert not await hub.pending_message_signal("unknown-worker")
    if kind == "human":
        await command(hub, "message.send", {
            "worker_id": worker_id, "message_id": "directed", "text": "Read the exact observation."})
        unread = hub.human_unread
    else:
        await open_collaboration(hub, task["id"])
        peer = await command(hub, "worker.register")
        second = await command(hub, "task.claim", {"task_id": task["id"], "revision": 1,
            "worker_id": peer["id"], "run_id": run["id"], "plan": "Check the related material."})
        await command(hub, "talk.send", {"recipient_id": worker_id, "message_id": "directed",
                      "text": "Read the exact observation."}, actor(second))
        unread = hub.peer_unread
    assert await hub.pending_message_signal(worker_id)
    page = await unread(actor(first), reference_only=True)
    input_id = TranscriptStore(hub.root).prepare_input(
        worker_id, "thread", "directed_messages", "A concrete observation.",
        kind="steer", expected_turn_id="turn")
    # Merely preparing a page or checking its signal does not acknowledge it.
    assert await hub.pending_message_signal(worker_id)
    await hub.mark_delivery_uncertain(Actor(role=Role.RUNNER), kind, page["delivery_id"],
                                     "thread", "turn", input_id, pending=True)
    assert not await hub.pending_message_signal(worker_id)
    await hub.confirm_delivery_transport(Actor(role=Role.RUNNER), kind, page["delivery_id"],
        False, thread_id="thread", turn_id="turn", input_id=input_id)
    assert await hub.pending_message_signal(worker_id)
    await command(hub, "message.ack" if kind == "human" else "talk.ack",
                  {"delivery_id": page["delivery_id"]}, actor(first))
    assert not await hub.pending_message_signal(worker_id)


async def test_empty_steering_skips_expensive_inboxes_but_new_message_is_delivered(project, monkeypatch):
    hub, _, run, first = project
    entered, empty_checked, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = {"human": 0, "peer": 0, "empty": 0}

    class Harness(ScriptedHarness):
        active_turn_id = None

        async def run_turn(self, prompt, *, on_started, **kwargs):
            self.prompts.append(prompt)
            self.active_turn_id = "one-turn"
            await on_started(self.active_turn_id)
            entered.set()
            await received.wait()
            self.active_turn_id = None
            await self.close()
            return TurnOutcome("one-turn", "completed", True, True, visible_output="Received.")

        async def steer(self, text, *, expected_turn_id, client_input_id):
            received.set()
            return SteerOutcome("accepted", expected_turn_id)

    harness = Harness()
    runner = WorkerRunner(hub, lambda: harness)
    execution = asyncio.create_task(runner.run_once(run["id"], assignment=first))
    try:
        async with asyncio.timeout(15):
            await entered.wait()
            original_human, original_peer = hub.human_unread, hub.peer_unread
            original_controls = hub.activation_controls

            async def human(*args, **kwargs):
                calls["human"] += 1
                return await original_human(*args, **kwargs)

            async def peer(*args, **kwargs):
                calls["peer"] += 1
                return await original_peer(*args, **kwargs)

            async def controls(watches):
                views = await original_controls(watches)
                view = views.get((run["id"], first["worker_id"]))
                if view is not None and not view["messages"]:
                    calls["empty"] += 1  # A control read that finds the inbox empty.
                    if calls["empty"] >= 3:
                        empty_checked.set()
                return views

            monkeypatch.setattr(hub, "human_unread", human)
            monkeypatch.setattr(hub, "peer_unread", peer)
            monkeypatch.setattr(hub, "activation_controls", controls)
            await empty_checked.wait()
            assert calls["human"] == calls["peer"] == 0
            await command(hub, "message.send", {"worker_id": first["worker_id"],
                "message_id": "arrives-later", "text": "A new concrete observation."})
            result = await execution
            assert result["turn_status"] == "completed"
            assert calls["human"] == calls["peer"] == 1
            assert not await hub.pending_message_signal(first["worker_id"])
            assert len(harness.prompts) == 1
    finally:
        received.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_a_message_sent_during_pause_after_turn_is_steered_into_the_finishing_turn(project):
    hub, _, run, first = project
    entered, received = asyncio.Event(), asyncio.Event()

    class Harness(ScriptedHarness):
        active_turn_id = None

        async def run_turn(self, prompt, *, on_started, **kwargs):
            self.prompts.append(prompt)
            self.active_turn_id = "one-turn"
            await on_started(self.active_turn_id)
            entered.set()
            await received.wait()
            self.active_turn_id = None
            await self.close()
            return TurnOutcome("one-turn", "completed", True, True, visible_output="Wrapping up.")

        async def steer(self, text, *, expected_turn_id, client_input_id):
            received.set()
            return SteerOutcome("accepted", expected_turn_id)

    harness = Harness()
    execution = asyncio.create_task(WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=first))
    try:
        async with asyncio.timeout(15):
            await entered.wait()
            await command(hub, "run.pause_after_turn", {"run_id": run["id"]})
            await command(hub, "message.send", {"worker_id": first["worker_id"], "message_id": "wrap-up",
                                                "text": "You may end this turn now."})
            assert (await execution)["turn_status"] == "completed"
            # Delivered into the turn under way, not left for a turn after the pause.
            assert received.is_set() and not await hub.pending_message_signal(first["worker_id"])
    finally:
        received.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_pause_after_turn_steers_a_persons_message_but_a_peers_waits(project):
    hub, task, run, first = project
    await open_collaboration(hub, task["id"])
    peer = await command(hub, "worker.register")
    second = await command(hub, "task.claim", {"task_id": task["id"], "revision": 1,
        "worker_id": peer["id"], "run_id": run["id"], "plan": "Check the related material."})
    entered, received = asyncio.Event(), asyncio.Event()
    steered = []

    class Harness(ScriptedHarness):
        active_turn_id = None

        async def run_turn(self, prompt, *, on_started, **kwargs):
            self.prompts.append(prompt)
            self.active_turn_id = "one-turn"
            await on_started(self.active_turn_id)
            entered.set()
            await received.wait()
            self.active_turn_id = None
            await self.close()
            return TurnOutcome("one-turn", "completed", True, True, visible_output="Wrapping up.")

        async def steer(self, text, *, expected_turn_id, client_input_id):
            steered.append(json.loads(text))
            received.set()
            return SteerOutcome("accepted", expected_turn_id)

    harness = Harness()
    execution = asyncio.create_task(WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=first))
    try:
        async with asyncio.timeout(15):
            await entered.wait()
            # Only this worker drains; its peer still runs and writes to it.
            await command(hub, "worker.pause_after_turn", {"worker_id": first["worker_id"]})
            await command(hub, "talk.send", {"recipient_id": first["worker_id"], "message_id": "peer-note",
                          "text": "A related observation."}, actor(second))
            await asyncio.sleep(1.5)
            assert steered == []  # The peer's message waits for a later turn.
            await command(hub, "message.send", {"worker_id": first["worker_id"], "message_id": "wrap-up",
                                                "text": "You may end this turn now."})
            assert (await execution)["turn_status"] == "completed"
            assert [[item.get("sender_role") for item in packet["messages"]] for packet in steered] == [["operator"]]
            # The peer's message still waits; the operator's was delivered.
            assert await hub.pending_message_signal(first["worker_id"])
            assert not await hub.pending_message_signal(first["worker_id"], human=True)
    finally:
        received.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_a_withdrawn_message_is_never_delivered_and_wakes_no_one(project):
    hub, _, _, first = project
    worker_id = first["worker_id"]
    sent = await command(hub, "message.send", {"worker_id": worker_id, "message_id": "wrap-up",
                                               "text": "You may end this turn now."})
    assert await hub.pending_message_signal(worker_id)
    withdrawn = await command(hub, "message.withdraw", {"message_id": sent["id"], "reason": "Sent during the drain"})
    assert not await hub.pending_message_signal(worker_id)
    page = await hub.human_unread(actor(first), reference_only=True)
    assert page["messages"] == [] and page["delivery_id"] is None
    assert await hub.db.read(lambda c: c.execute("SELECT COUNT(*) FROM worker_wake_messages WHERE message_id=?",
                                                 (sent["id"],)).fetchone()[0]) == 0
    # Withdrawing again changes nothing; the history shows when it was withdrawn.
    assert await command(hub, "message.withdraw", {"message_id": sent["id"]}) == withdrawn
    history = await hub.human_history(worker_id)
    assert history["messages"][-1]["withdrawn_at"] == withdrawn["withdrawn_at"]
    assert history["withdrawals"] == [{"id": sent["id"], "withdrawn_at": withdrawn["withdrawn_at"]}]
    # Once a delivery of a message is prepared it may already be in a turn: it stays.
    await command(hub, "message.send", {"worker_id": worker_id, "message_id": "read", "text": "Read this."})
    page = await hub.human_unread(actor(first), reference_only=True)
    with pytest.raises(HubError) as refused:
        await command(hub, "message.withdraw", {"message_id": page["messages"][0]["id"]})
    assert refused.value.code == "MESSAGE_DELIVERED"
    # Only the operator withdraws, and only its own messages.
    with pytest.raises(HubError):
        await command(hub, "message.withdraw", {"message_id": sent["id"]}, actor(first))


def test_codex_history_records_a_steered_input_by_its_client_id(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    stored = home / "rollout.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "thread"}},
        {"timestamp": "2026-10-18T21:35:18.420Z", "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "thread", "turn_id": "turn-1",
            "item": {"type": "UserMessage", "id": "item", "client_id": "a" * 32, "content": []}}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "b" * 32 + " is only quoted"}},
    ]
    stored.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    harness = CodexHarness(CodexConfig(home=str(home)), command=["unused"])
    assert harness.recorded_input("a" * 32) is None  # No session file is known yet.
    harness.session_path = str(stored)
    assert harness.recorded_input("a" * 32) == ("2026-10-18T21:35:18.420Z", "turn-1")
    assert harness.recorded_input("b" * 32) is None  # Mentioned, but never an added input.
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(stored.read_text(encoding="utf-8"), encoding="utf-8")
    harness.session_path = str(outside)
    assert harness.recorded_input("a" * 32) is None  # Only files inside the Codex home are read.


async def test_a_timed_out_steer_is_confirmed_once_native_history_records_it(project, monkeypatch):
    hub, _, run, first = project
    entered, steered, recorded, finished = (asyncio.Event() for _ in range(4))
    monkeypatch.setattr(runner_module, "STEER_SETTLE_SECONDS", 0.05)

    class Harness(ScriptedHarness):
        active_turn_id = None

        def __init__(self):
            super().__init__()
            self.inputs = []

        async def run_turn(self, prompt, *, on_started, **kwargs):
            self.prompts.append(prompt)
            self.active_turn_id = "one-turn"
            await on_started(self.active_turn_id)
            entered.set()
            await finished.wait()
            self.active_turn_id = None
            await self.close()
            return TurnOutcome("one-turn", "completed", True, True, visible_output="Done.")

        async def steer(self, text, *, expected_turn_id, client_input_id):
            self.inputs.append(client_input_id)
            steered.set()
            return SteerOutcome("uncertain", expected_turn_id,
                                "RPC request timed out; remote outcome is unknown: turn/steer")

        def recorded_input(self, client_input_id):
            if recorded.is_set() and client_input_id in self.inputs:
                return "2026-10-18T21:35:18.420Z", "one-turn"
            return None

    harness = Harness()
    execution = asyncio.create_task(WorkerRunner(hub, lambda: harness).run_once(run["id"], assignment=first))
    worker_id = first["worker_id"]

    async def holds():
        return await hub.db.read(lambda c: [tuple(row) for row in c.execute(
            "SELECT kind,state,reason FROM message_delivery_holds")])
    try:
        async with asyncio.timeout(20):
            await entered.wait()
            await command(hub, "message.send", {"worker_id": worker_id, "message_id": "late",
                                                "text": "Read this concrete observation."})
            await steered.wait()
            while (await holds())[0][1] != "uncertain":
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)  # Settling passes find no record yet; the hold stays.
            assert [hold[:2] for hold in await holds()] == [("human", "uncertain")]
            assert not await hub.pending_message_signal(worker_id)  # Held, so never resent.
            recorded.set()
            while (await holds())[0][1] == "uncertain":
                await asyncio.sleep(0.02)
            kind, state, reason = (await holds())[0]
            assert state == "delivered" and "native history records this input" in reason
            assert len(harness.inputs) == 1 and not await hub.pending_message_signal(worker_id)
            finished.set()
            assert (await execution)["turn_status"] == "completed"
    finally:
        finished.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
