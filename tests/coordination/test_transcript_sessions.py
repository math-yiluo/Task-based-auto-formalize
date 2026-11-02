"""Read-only conversation projections from exact native transcript files."""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from formal_geyser.coordination import transcripts as module
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.transcripts import TranscriptStore


def native(store, method, turn_id="t1", thread_id="main", **params):
    store.capture_native(thread_id, "w", method, {"turnId": turn_id, **params})


def started(store, turn_id="t1", thread_id="main"):
    native(store, "turn/started", turn_id, thread_id,
           turn={"id": turn_id, "status": "inProgress"})


def completed(store, turn_id="t1", thread_id="main", status="completed"):
    native(store, "turn/completed", turn_id, thread_id,
           turn={"id": turn_id, "status": status})


def reply(store, text, turn_id="t1", item_id="reply", thread_id="main"):
    native(store, "item/completed", turn_id, thread_id,
           item={"id": item_id, "type": "agentMessage", "text": text, "phase": "final_answer"})


def test_live_deltas_final_replacement_and_explicit_terminal(tmp_path):
    store = TranscriptStore(tmp_path)
    exact = "English prompt, Unicode ∀ x, 世界\n" * 1000
    input_id = store.prepare_input("w", "main", "taskhub", exact, kind="initial")
    started(store)
    native(store, "item/agentMessage/delta", itemId="r", delta="First ")
    native(store, "item/agentMessage/delta", itemId="r", delta="answer")
    first = store.session_page("w", "thread:main")
    turn = first["turns"][0]
    assert turn["inputs"][0]["text"] == exact
    assert turn["inputs"][0]["status"] == "prepared"
    assert turn["replies"][0]["text"] == "First answer"
    assert turn["replies"][0]["status"] == "streaming"
    assert turn["status"] == "in_progress"

    # Delivery receipt may arrive after native output without creating a turn.
    store.delivered(input_id, "t1")
    reply(store, "Final exact answer\n", item_id="r")
    reply(store, "Final exact answer\n", item_id="r")
    native(store, "item/agentMessage/delta", itemId="r", delta="late duplicate")
    second = store.session_page("w", "thread:main")
    assert len(second["turns"]) == 1
    assert second["turns"][0]["key"] == turn["key"]
    assert second["turns"][0]["status"] == "in_progress"
    assert second["turns"][0]["inputs"][0]["status"] == "delivered"
    assert second["turns"][0]["replies"][0]["text"] == "Final exact answer\n"
    assert len(second["turns"][0]["replies"]) == 1
    completed(store)
    final = store.session_page("w", "thread:main")["turns"][0]
    assert final["status"] == "completed" and final["completed_at"]
    assert final["ordinal"] == turn["ordinal"]


def test_initial_unbound_input_adopts_real_thread_without_losing_key(tmp_path):
    store = TranscriptStore(tmp_path)
    store.record_instructions("w", None, "Initial standing instruction")
    input_id = store.prepare_input("w", None, "taskhub", "Exact first prompt")
    old_session = store.sessions("w")["sessions"][0]
    old = store.session_page("w", old_session["key"])
    started(store)
    reply(store, "Reply before input binding")
    store.delivered(input_id, "t1")
    session = store.sessions("w")["sessions"][0]
    assert session["key"] == "thread:main" and session["turn_count"] == 1
    page = store.session_page("w", old_session["key"])
    assert page["session"]["key"] == "thread:main"
    assert page["turns"][0]["key"] == old["turns"][0]["key"]
    assert page["turns"][0]["ordinal"] == 1
    assert page["turns"][0]["inputs"][0]["text"] == "Exact first prompt"
    assert page["turns"][0]["replies"][0]["text"] == "Reply before input binding"
    assert len(page["context"]) == 1

    # A later unknown native session must not inherit an old generic alias.
    second = store.prepare_input("w", None, "taskhub", "Different future session")
    store.delivered(second, "other-turn", thread_id="other")
    assert {item["thread_id"] for item in store.sessions("w")["sessions"]} == {"main", "other"}


def test_orphan_turn_binding_exposes_superseded_key(tmp_path):
    store = TranscriptStore(tmp_path)
    # Two pending starts are ambiguous until an exact receipt identifies one.
    input_a = store.prepare_input("w", "main", "taskhub", "First prepared input")
    input_b = store.prepare_input("w", "main", "taskhub", "Second prepared input")
    started(store)
    reply(store, "Native orphan reply")
    before = store.session_page("w", "thread:main")
    orphan = next(turn for turn in before["turns"] if turn["turn_id"] == "t1")
    assert not orphan["inputs"]
    store.delivered(input_a, "t1")
    after = store.session_page("w", "thread:main")
    assert after["superseded_turn_keys"] == [orphan["key"]]
    assert len(after["turns"]) == 2
    merged = next(turn for turn in after["turns"] if turn["turn_id"] == "t1")
    assert merged["key"] == "input:" + input_a
    assert merged["replies"][0]["text"] == "Native orphan reply"
    assert next(turn for turn in after["turns"] if turn["key"] == "input:" + input_b)["status"] == "prepared"


def test_continuations_pagination_and_latest_poll_have_stable_keys(tmp_path):
    store = TranscriptStore(tmp_path)
    for i in range(47):
        packet = {"task": {"id": "task-a", "revision": 4, "contract": {"title": "English task", "kind": "ordinary"}}}
        input_id = store.prepare_input("w", "main", "taskhub", json.dumps(packet), kind="continuation")
        store.delivered(input_id, f"turn-{i}")
        reply(store, f"Reply {i}", f"turn-{i}", f"reply-{i}")
        completed(store, f"turn-{i}")
    latest = store.session_page("w", "thread:main")
    assert latest["session"]["turn_count"] == 47
    assert [turn["ordinal"] for turn in latest["turns"]] == list(range(28, 48))
    assert latest["next_before"] == 28
    older = store.session_page("w", "thread:main", before=latest["next_before"])
    oldest = store.session_page("w", "thread:main", before=older["next_before"])
    all_turns = oldest["turns"] + older["turns"] + latest["turns"]
    assert len({turn["key"] for turn in all_turns}) == 47
    assert [turn["ordinal"] for turn in all_turns] == list(range(1, 48))
    assert not oldest["has_more"] and oldest["next_before"] is None
    assert all_turns[0]["task"] == {"id": "task-a", "title": "English task", "revision": 4, "kind": "ordinary"}
    exact_old_keys = [turn["key"] for turn in latest["turns"]]
    new = store.prepare_input("w", "main", "taskhub", "Live continuation")
    store.delivered(new, "turn-new")
    polled = store.session_page("w", "thread:main", include_context=False)
    assert [turn["key"] for turn in polled["turns"][:-1]] == exact_old_keys[1:]
    assert polled["turns"][-1]["ordinal"] == 48
    assert polled["context"] == []


def test_instruction_versions_steer_delivery_and_interrupted_partial_reply(tmp_path):
    store = TranscriptStore(tmp_path)
    store.record_instructions("w", "main", "Initial instructions")
    initial = store.prepare_input("w", "main", "taskhub", "Original input")
    store.delivered(initial, "t1")
    steer = store.prepare_input("w", "main", "directed_messages", "Exact directed text",
                                kind="steer", expected_turn_id="t1")
    store.ambiguous(steer, "Lost response")
    native(store, "item/agentMessage/delta", itemId="part", delta="Partial visible reply")
    completed(store, status="interrupted")
    store.reconciled(steer, delivered=False, turn_id="t1", reason="Proven not accepted")
    store.record_instructions("w", "main", "Changed instructions")
    store.prepare_input("w", "main", "taskhub", "Next prompt")
    page = store.session_page("w", "thread:main")
    first, second = page["turns"]
    assert first["status"] == "interrupted"
    assert [record["text"] for record in first["inputs"]] == ["Original input", "Exact directed text"]
    assert first["inputs"][1]["status"] == "not_delivered"
    assert first["replies"][0]["status"] == "incomplete"
    assert first["instructions_key"] != second["instructions_key"]
    assert page["session"]["context_version"] == 2


def test_ambiguous_native_turn_is_incomplete_without_inventing_completion(tmp_path):
    store = TranscriptStore(tmp_path)
    initial = store.prepare_input("w", "main", "taskhub", "Known input")
    store.delivered(initial, "t1")
    native(store, "item/agentMessage/delta", itemId="partial", delta="Before transport loss")
    store.record_uncertain_turn("w", "main", "t1", initial)
    turn = store.session_page("w", "thread:main")["turns"][0]
    assert turn["status"] == "uncertain" and turn["completed_at"] is None
    assert turn["replies"][0]["status"] == "incomplete"
    assert turn["inputs"][0]["status"] == "delivered"


def test_cache_shared_append_only_and_torn_tail_recovery(tmp_path, monkeypatch):
    store = TranscriptStore(tmp_path)
    store.prepare_input("w", "main", "taskhub", "Original")
    applied = []
    original = module._TranscriptView.apply

    def count(view, event):
        applied.append(event["id"])
        return original(view, event)

    monkeypatch.setattr(module._TranscriptView, "apply", count)
    assert store.sessions("w")["sessions"][0]["turn_count"] == 1
    clone = TranscriptStore(tmp_path)
    for _ in range(5):
        clone.session_page("w", "thread:main", include_context=False)
    assert len(applied) == 1
    started(store)
    clone.sessions("w")
    assert len(applied) == 2
    path = next(store.root.rglob("events.jsonl"))
    with path.open("ab") as stream:
        stream.write(b'{"torn":')
    assert clone.session_page("w", "thread:main")["turns"][0]["status"] == "in_progress"
    assert len(applied) == 2
    reply(store, "Recovered append")
    assert clone.session_page("w", "thread:main")["turns"][0]["replies"][0]["text"] == "Recovered append"
    assert len(applied) == 3


def test_cache_invalidates_replacement_and_return_values_are_copies(tmp_path):
    store = TranscriptStore(tmp_path)
    store.prepare_input("w", "main", "taskhub", "Prompt one")
    page = store.session_page("w", "thread:main")
    page["turns"][0]["inputs"][0]["text"] = "Caller mutation"
    assert store.session_page("w", "thread:main")["turns"][0]["inputs"][0]["text"] == "Prompt one"
    path = next(store.root.rglob("events.jsonl"))
    text = path.read_text(encoding="utf-8").replace("Prompt one", "Prompt two")
    path.write_text(text, encoding="utf-8")
    assert store.session_page("w", "thread:main")["turns"][0]["inputs"][0]["text"] == "Prompt two"
    path.write_text("", encoding="utf-8")
    assert store.sessions("w") == {"sessions": []}


def test_read_only_missing_history_and_visible_actions_filter_streams(tmp_path):
    store = TranscriptStore(tmp_path)
    assert store.sessions("missing") == {"sessions": []}
    assert not store.root.exists()
    with pytest.raises(HubError, match="does not exist"):
        store.session_page("missing", "thread:anything")
    store.prepare_input("w", "main", "taskhub", "Work")
    started(store)
    action = {"id": "command", "type": "commandExecution", "command": "lake build", "cwd": "workspace",
              "aggregatedOutput": "Omit enormous compiler stdout", "status": "inProgress"}
    native(store, "item/started", item=action)
    native(store, "item/completed", item={**action, "status": "completed", "exitCode": 0})
    native(store, "item/completed", item={"id": "hidden", "type": "reasoning", "text": "Hidden reasoning"})
    store.record_action("w", "main", "t1", "tool", "taskhub_stage", "running", arguments={"manifest_path": "submission.json"})
    store.record_action("w", "main", "t1", "tool", "taskhub_stage", "completed", result={"artifact_id": "a"})
    page = store.session_page("w", "thread:main")
    assert len(page["turns"][0]["actions"]) == 2
    assert page["turns"][0]["actions"][0]["details"]["exitCode"] == 0
    assert page["turns"][0]["actions"][1]["arguments"] == {"manifest_path": "submission.json"}
    assert "compiler stdout" not in json.dumps(page) and "Hidden reasoning" not in json.dumps(page)
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: TranscriptStore(tmp_path).session_page("w", "thread:main"), range(12)))
    assert before == {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("arguments", [{"before": 0}, {"before": True}, {"limit": 0},
                                      {"limit": 101}, {"include_context": "false"}])
def test_bad_projection_cursors_are_rejected(tmp_path, arguments):
    with pytest.raises(HubError) as error:
        TranscriptStore(tmp_path).session_page("w", "thread:main", **arguments)
    assert error.value.code == "INVALID_INPUT"


def test_streamed_text_is_written_in_timed_chunks(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module, "_clock", lambda: now[0])
    store = TranscriptStore(tmp_path)
    started(store)
    for part in ("One ", "two "):
        native(store, "item/agentMessage/delta", itemId="r", delta=part)
    now[0] += module.DELTA_FLUSH_SECONDS
    native(store, "item/agentMessage/delta", itemId="r", delta="three ")
    native(store, "item/agentMessage/delta", itemId="r", delta="four")
    # A read first writes the text collected so far.
    streaming = store.session_page("w", "thread:main")["turns"][0]["replies"][0]
    assert (streaming["text"], streaming["status"]) == ("One two three four", "streaming")
    events = store.page("w")["events"]
    assert [e["text"] for e in events if e["type"] == "assistant_delta"] == ["One two three ", "four"]
    reply(store, "Final", item_id="r")
    native(store, "item/agentMessage/delta", itemId="cut", delta="Cut off")
    completed(store, status="interrupted")
    replies = store.session_page("w", "thread:main")["turns"][0]["replies"]
    assert [(r["text"], r["status"]) for r in replies] == [("Final", "completed"), ("Cut off", "incomplete")]
    assert store._directory("w") not in module._STREAMS


def test_activity_is_queued_in_order_and_a_synchronous_record_follows_it(tmp_path, monkeypatch):
    caller = threading.current_thread()
    writers = []
    original = module._write_lines

    def write(directory, lines):
        writers.append(threading.current_thread())
        original(directory, lines)

    monkeypatch.setattr(module, "_write_lines", write)
    store = TranscriptStore(tmp_path)
    started(store)
    store.record_action("w", "main", "t1", "call", "taskhub_call", "running")
    for _ in range(50):
        native(store, "item/agentMessage/delta", itemId="r", delta="x")
    assert caller not in writers  # Visible activity never waits for a sync on the caller.
    store.prepare_input("w", "main", "taskhub", "Steer", kind="steer")
    assert writers.count(caller) == 1  # An input is synced at once, after what preceded it.
    store.record_action("w", "main", "t1", "call", "taskhub_call", "completed")
    store.flush()
    events = store.page("w")["events"]
    assert [(e["type"], e.get("status")) for e in events] == [
        ("turn", "inProgress"), ("action", "running"), ("assistant_delta", None),
        ("input", "prepared"), ("action", "completed")]
    assert events[2]["text"] == "x" * 50


def test_a_failed_activity_write_is_logged_without_reaching_the_caller(tmp_path, monkeypatch, caplog):
    def fail(directory, lines):
        raise OSError("Synthetic full disk")

    monkeypatch.setattr(module, "_write_lines", fail)
    store = TranscriptStore(tmp_path)
    with caplog.at_level(logging.ERROR, logger=module.__name__):
        store.record_action("w", "main", "t1", "call", "taskhub_call", "running")
        store.flush()
    assert "could not be written" in caplog.text
    with pytest.raises(OSError):
        store.prepare_input("w", "main", "taskhub", "Inputs still fail loudly")
