"""Delivery-bound continuation omission without a native process or model."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from formal_geyser.coordination.runtime import continuation as module
from formal_geyser.coordination.runtime.continuation import ContinuationPromptStore
from formal_geyser.coordination.runtime.model_view import project_packet

WORKER = "raw-worker-identity"
THREAD = "native-main-thread"


def packet():
    return {
        "packet_id": "packet-1", "worker_id": "Worker 4", "context_epoch": 3,
        "task": {"id": "task-alpha", "revision": 1, "title": "Task Alpha", "kind": "work",
                 "objective": "Exact objective ∀ x, α ≤ β\n" * 30,
                 "context": "Current context", "constraints": ["Preserve exact evidence"],
                 "payload": {"allow": False, "count": 0}},
        "native": {"workspace": "workspace/worker-4", "private_notes": "private/notes.md",
                   "directories": {"library": "library", "wiki": "wiki"},
                   "library": {"commit": "commit-a", "root": "public/a"}},
        "worker_identity": {"worker_id": "Worker 4", "display_name": "Worker 4"},
        "inputs": [{"id": "artifact-a", "status": "accepted"}],
        "requirements": [{"task_id": "dependency", "satisfied": False, "artifact_ids": []}],
        "resume_requirements": [],
        "subscriptions": {"count": 1, "tasks": [{"id": "dependency", "state": "open"}]},
        "future_field": {"exact": "Future context must survive"},
        "attempt": {"id": "attempt-1", "fencing_token": 1, "snapshot": "snapshot-a"},
        "checkpoint": {"summary": "Already saved"}, "execution": {"active_workers": 2},
        "usage": {"turns_used": 1, "tokens_used": 100},
        "after_sequence": 4, "through_sequence": 4, "updates": [],
        "has_more_updates": False, "requires_sync": False,
        "material_access": "Use the existing material tools.",
        "continuation_instruction": "Continue the current task in this original conversation.",
    }


def current_worker(*, worker_id=WORKER, thread_id=THREAD, task_id="task-alpha", epoch=3):
    return {"id": worker_id, "thread_id": thread_id, "current_task_id": task_id,
            "context_epoch": epoch}


def prepare(store, value=None, **kwargs):
    return store.prepare(packet() if value is None else value,
                         worker_id=kwargs.pop("worker_id", WORKER),
                         thread_id=kwargs.pop("thread_id", THREAD), **kwargs)


def seed(store, value=None):
    prepared = prepare(store, value)
    assert prepared.mode == "full"
    assert store.confirm(prepared, current_worker=current_worker())
    return prepared


def baseline_path(root):
    return root / ".task-based" / "prompt-contexts" / (
        hashlib.sha256(WORKER.encode()).hexdigest() + ".json")


def apply_delta(previous, delta):
    """Interpret the public field-replacement format as a receiving worker would."""
    result = deepcopy(previous)
    result["task"].update(delta["task"])
    for change in delta.get("changes", []):
        target = result
        for key in change["path"][:-1]:
            target = target[key]
        target[change["path"][-1]] = deepcopy(change["value"])
    for path in delta.get("removed_fields", []):
        target = result
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return result


def test_confirmed_same_conversation_is_compact_across_restart(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    original = packet()
    first = seed(store, original)
    assert json.loads(first.text) == project_packet(original)
    assert (first.worker_id, first.thread_id, first.task_id, first.context_epoch) == (
        WORKER, THREAD, "task-alpha", 3)

    store = ContinuationPromptStore(tmp_path)
    second = prepare(store)
    body = json.loads(second.text)
    assert second.mode == "delta"
    assert body["task"] == {key: original["task"][key]
                            for key in ("id", "revision", "title", "kind")}
    assert body.get("changes", []) == []
    assert body.get("removed_fields", []) == []
    assert original["task"]["objective"] not in second.text
    assert len(second.text.encode()) < len(first.text.encode()) // 2
    assert store.confirm(second, current_worker=current_worker())
    assert store.confirm(second, current_worker=current_worker())
    assert prepare(store).mode == "delta"


def test_pending_delivery_never_reuses_an_older_confirmed_baseline(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    uncertain = packet()
    uncertain["task"]["context"] = "This may already have reached the worker."
    assert prepare(store, uncertain).mode == "delta"

    # The caller crashed or could not establish transport acceptance. Returning
    # to the old value must state it explicitly rather than omit it as unchanged.
    recovered = prepare(ContinuationPromptStore(tmp_path))
    assert recovered.mode == "full"
    assert json.loads(recovered.text) == project_packet(packet())


def test_initial_preparation_is_not_a_delivery_receipt(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    first = prepare(store)
    second = prepare(ContinuationPromptStore(tmp_path))
    assert first.mode == second.mode == "full"
    assert first.token != second.token


@pytest.mark.parametrize("pending", [
    {"has_more_updates": True}, {"requires_sync": True},
    {"human_messages": {"messages": [], "has_more": True}},
    {"private_talk": {"messages": [], "has_more": True}},
])
def test_pending_delivery_hint_never_claims_there_are_no_new_deliveries(tmp_path, pending):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    prepared = prepare(store, {**packet(), **pending})
    body = json.loads(prepared.text)
    assert prepared.mode == "delta"
    assert "sync" in body["next_action"]
    assert "No new context changes or deliveries" not in prepared.text


def test_explicit_full_context_updates_baseline_before_state_reversal(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    synced = packet()
    synced["task"]["context"] = "State B delivered by a full synchronization."
    full = prepare(store, synced, allow_delta=False)
    assert full.mode == "full"
    assert json.loads(full.text) == project_packet(synced)
    assert store.confirm(full, current_worker=current_worker())

    restored = prepare(store)
    assert restored.mode == "delta"
    interpreted = apply_delta(project_packet(synced), json.loads(restored.text))
    assert interpreted["task"]["context"] == packet()["task"]["context"]


@pytest.mark.parametrize("binding", ["worker", "thread", "task", "epoch"])
def test_changed_binding_requires_full_context(tmp_path, binding):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    value = packet()
    kwargs = {}
    if binding == "worker":
        kwargs["worker_id"] = "another-worker"
    elif binding == "thread":
        kwargs["thread_id"] = "new-native-thread"
    elif binding == "task":
        value["task"]["id"] = "same-family-different-task"
    else:
        value["context_epoch"] += 1
    changed = prepare(store, value, **kwargs)
    assert changed.mode == "full"
    assert json.loads(changed.text) == project_packet(value)


def test_returning_to_a_previous_task_does_not_reuse_its_historical_baseline(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    other = packet()
    other["task"]["id"] = "task-beta"
    changed = prepare(store, other)
    assert store.confirm(changed, current_worker=current_worker(task_id="task-beta"))
    assert prepare(store).mode == "full"


@pytest.mark.parametrize("field,value", [
    ("id", "other-worker"), ("thread_id", "other-thread"),
    ("current_task_id", "other-task"), ("context_epoch", 4),
])
def test_confirmation_requires_current_exact_binding(tmp_path, field, value):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    candidate = prepare(store)
    worker = current_worker()
    worker[field] = value
    assert not store.confirm(candidate, current_worker=worker)
    assert prepare(store).mode == "full"


def test_invalidation_prevents_a_late_receipt_from_restoring_baseline(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    candidate = prepare(store)
    store.invalidate(WORKER)
    assert not store.confirm(candidate, current_worker=current_worker())
    assert prepare(ContinuationPromptStore(tmp_path)).mode == "full"


@pytest.mark.parametrize("corrupt", [
    "{", "[]", "{}", '{"format":2,"status":"confirmed"}',
    '{"format":1,"status":"confirmed","snapshot":[],"snapshot_sha256":"bad"}',
    '{"format":1,"status":"confirmed","snapshot":{},"snapshot_sha256":"bad"}',
    '{"format":1,"status":"confirmed","snapshot":{"bad":NaN},"snapshot_sha256":"bad"}',
])
def test_corrupt_or_unsupported_baseline_falls_back_to_full(tmp_path, corrupt):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    baseline_path(tmp_path).write_text(corrupt, encoding="utf-8")
    recovered = prepare(ContinuationPromptStore(tmp_path))
    assert recovered.mode == "full"
    assert json.loads(recovered.text) == project_packet(packet())


def test_input_mutation_after_preparation_cannot_change_confirmed_snapshot(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    value = packet()
    original = deepcopy(value)
    candidate = prepare(store, value)
    recorded = candidate.snapshot_json
    value["task"]["context"] = "Never sent"
    value["requirements"][0]["artifact_ids"].append("never-sent-artifact")
    assert candidate.snapshot_json == recorded
    assert json.loads(candidate.text) == project_packet(original)
    assert store.confirm(candidate, current_worker=current_worker())
    same = prepare(store, original)
    assert same.mode == "delta"
    assert json.loads(same.text).get("changes", []) == []


def test_changed_task_dependencies_survive_and_unknown_backend_fields_stay_private(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    original = packet()
    seed(store, original)
    updated = deepcopy(original)
    updated["task"].update(revision=2, objective="Revised exact objective λ\n")
    updated["native"]["library"].update(commit="commit-b", root="public/b")
    updated["inputs"] = []
    updated["requirements"] = [{"task_id": "dependency", "satisfied": True,
                                "artifact_ids": ["accepted-result"]}]
    updated["resume_requirements"] = [{"artifact_id": "accepted-result", "satisfied": True}]
    updated["future_field"]["exact"] = "New field content survives without an allowlist."
    body = json.loads(prepare(store, updated).text)
    assert body["task"]["revision"] == 2
    assert apply_delta(project_packet(original), body) == project_packet(updated)
    assert "future_field" not in str(body)
    list_paths = {tuple(change["path"]) for change in body.get("changes", [])
                  if isinstance(change["value"], list)}
    assert {("requirements",), ("resume_requirements",)} <= list_paths
    assert ["inputs"] in body["removed_fields"]


def test_removals_and_null_empty_false_zero_are_distinct(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    previous = packet()
    seed(store, previous)
    updated = deepcopy(previous)
    del updated["subscriptions"]
    del updated["native"]["directories"]["library"]
    candidate = prepare(store, updated)
    body = json.loads(candidate.text)
    assert {tuple(path) for path in body.get("removed_fields", [])} == {
        ("paths", "reference_index", "library"),}
    assert apply_delta(project_packet(previous), body) == project_packet(updated)
    assert store.confirm(candidate, current_worker=current_worker())
    previous = updated

    for value in (None, "", [], {}, {"new": None}, {}, False, 0, True, 1):
        updated = deepcopy(previous)
        updated["task"]["payload"]["test_value"] = value
        candidate = prepare(store, updated)
        body = json.loads(candidate.text)
        interpreted = apply_delta(project_packet(previous), body)
        assert json.dumps(interpreted, sort_keys=True) == json.dumps(project_packet(updated), sort_keys=True)
        assert body.get("changes") or body.get("removed_fields")
        assert store.confirm(candidate, current_worker=current_worker())
        previous = updated


def test_runtime_bookkeeping_does_not_become_context_changes(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    value = packet()
    value.update(packet_id="new-packet", attempt={"id": "next-attempt", "fencing_token": 2},
                 checkpoint=None, execution={"active_workers": 7}, usage={"tokens_used": 200},
                 after_sequence=19, through_sequence=19, material_access="Updated material hint",
                 continuation_instruction="Repeated ordinary continuation guidance.")
    body = json.loads(prepare(store, value).text)
    assert body.get("changes", []) == []
    assert body.get("removed_fields", []) == []


@pytest.mark.parametrize("wiki_mode", ["pages", "removals", "both"])
def test_exact_messages_are_retained_and_obsolete_wiki_packages_are_omitted(tmp_path, wiki_mode):
    store = ContinuationPromptStore(tmp_path)
    value = packet()
    exact = "Exact directed material ∀ x, α ≤ β\n" * 300
    value["updates"] = [{"sequence": 5, "kind": "dependency.ready", "object_id": "dependency",
                         "payload": {"artifact_ids": ["accepted-result"], "details": exact}}]
    value["human_messages"] = {"delivery_id": "human-delivery", "has_more": True,
                               "messages": [{"id": "human-1", "text": exact, "offset": 100}]}
    value["private_talk"] = {"delivery_id": "peer-delivery", "messages": [
        {"id": "peer-1", "sender": "Worker 9", "text": exact, "offset": 0}]}
    value["required_wiki"] = {"delivery_id": "wiki-delivery", "commit": "commit-b",
                              "full_read": False, "instruction": "Read every supplied page.",
                              "pages": ([] if wiki_mode == "removals" else [
                                  {"path": "wiki/guide.md", "sha256": "exact-hash", "text": exact}]),
                              "removed": [] if wiki_mode == "pages" else ["wiki/removed.md"]}
    value.update(has_more_updates=True, requires_sync=True)
    seed(store, value)
    result = prepare(store, value)
    assert result.mode == "delta"
    body = json.loads(result.text)
    for field in ("notifications", "messages"):
        assert body[field] == project_packet(value)[field]
    assert "More updates or messages are available" in body["next_action"]
    assert "delivery_id" not in body and "packet_id" not in body
    assert "wiki" not in body
    assert body.get("changes", []) == []


def test_late_confirmation_invalidates_even_a_newer_confirmed_delivery(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    earlier_value = packet()
    earlier_value["task"]["context"] = "Earlier candidate"
    earlier = prepare(store, earlier_value)
    newer_value = packet()
    newer_value["task"]["context"] = "Newer full candidate"
    newer = prepare(store, newer_value)
    assert earlier.mode == "delta"
    assert newer.mode == "full"
    assert store.confirm(newer, current_worker=current_worker())
    assert not store.confirm(earlier, current_worker=current_worker())
    assert prepare(store, newer_value).mode == "full"


def test_concurrent_store_instances_cannot_share_a_confirmed_baseline(tmp_path):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    barrier = Barrier(6)

    def competing_prepare(number):
        value = packet()
        value["task"]["context"] = f"Candidate {number}"
        barrier.wait(timeout=10)
        return prepare(ContinuationPromptStore(tmp_path), value)

    with ThreadPoolExecutor(max_workers=6) as pool:
        competing = list(pool.map(competing_prepare, range(6)))
    assert len({candidate.token for candidate in competing}) == 6
    assert [candidate.mode for candidate in competing].count("delta") == 1
    assert [candidate.mode for candidate in competing].count("full") == 5

    latest = prepare(store)
    assert latest.mode == "full"
    for candidate in competing:
        assert not store.confirm(candidate, current_worker=current_worker())
    assert not store.confirm(latest, current_worker=current_worker())
    assert prepare(ContinuationPromptStore(tmp_path)).mode == "full"


def test_pending_write_failure_aborts_before_returning_sendable_input(tmp_path, monkeypatch):
    store = ContinuationPromptStore(tmp_path)
    seed(store)

    def unavailable(*args):
        raise OSError("Synthetic durable-write failure")

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_json", unavailable)
        with pytest.raises(OSError, match="durable-write"):
            prepare(store)
    assert prepare(store).mode == "delta"


def test_confirmation_write_failure_keeps_restart_in_full_mode(tmp_path, monkeypatch):
    store = ContinuationPromptStore(tmp_path)
    seed(store)
    candidate = prepare(store)

    def unavailable(*args):
        raise OSError("Synthetic confirmation failure")

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_json", unavailable)
        with pytest.raises(OSError, match="confirmation failure"):
            store.confirm(candidate, current_worker=current_worker())
    assert prepare(ContinuationPromptStore(tmp_path)).mode == "full"
