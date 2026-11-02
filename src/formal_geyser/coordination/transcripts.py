"""Durable visible native conversations, separate from sanitized operational logs.

Files are authoritative. Reading this store never resumes a model or acknowledges
an inbox. Native reasoning events are deliberately outside the visible transcript.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import threading
import time
from bisect import bisect_left
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import portalocker

from .errors import HubError
from .storage import atomic_json, canonical

logger = logging.getLogger(__name__)
_VIEW_LOCK = threading.RLock()
# A streaming reply's text is collected and written as one record per interval. Any other
# record of the worker, and any read of its history, first writes what has been collected.
DELTA_FLUSH_SECONDS = 2.0
_clock = time.monotonic
_VIEWS: OrderedDict[str, _TranscriptView] = OrderedDict()
_VIEW_CACHE_SIZE = 32
_TERMINAL_TURNS = {"completed", "failed", "interrupted", "cancelled", "not_delivered"}


def _status(value: Any) -> str:
    value = str(value or "pending")
    return "in_progress" if value in {"inProgress", "started", "running"} else value


class _SessionView:
    def __init__(self, key: str, event: dict):
        self.info: dict[str, Any] = {"key": key, "kind": "helper" if event.get("helper_id") else "main",
                     "thread_id": event.get("thread_id"), "helper_id": event.get("helper_id"),
                     "started_at": event.get("at"), "updated_at": event.get("at"),
                     "turn_count": 0, "last_status": "empty", "context_version": 0}
        self.turns: dict[str, dict] = {}
        self.native_turns: dict[str, str] = {}
        self.context: OrderedDict[str, dict] = OrderedDict()
        self.latest_instructions: str | None = None
        self.superseded_turn_keys: set[str] = set()
        self.next_ordinal = 1
        self._ordered: list[dict] | None = None

    def touch(self, at: str | None) -> None:
        if at:
            self.info["started_at"] = min(self.info["started_at"] or at, at)
            self.info["updated_at"] = max(self.info["updated_at"] or at, at)

    def new_turn(self, key: str, event: dict) -> dict:
        turn: dict[str, Any] = {"key": key, "ordinal": self.next_ordinal, "turn_id": event.get("turn_id"),
                "status": "pending", "started_at": event.get("at"), "updated_at": event.get("at"),
                "completed_at": None, "task": None, "instructions_key": self.latest_instructions,
                "inputs": OrderedDict(), "replies": OrderedDict(), "actions": OrderedDict(),
                "_native_status": False}
        self.next_ordinal += 1
        self.turns[key] = turn
        self._ordered = None
        return turn

    def ordered_turns(self) -> list[dict]:
        if self._ordered is None:
            self._ordered = sorted(self.turns.values(), key=lambda turn: turn["ordinal"])
        return self._ordered

    def metadata(self) -> dict:
        ordered = self.ordered_turns()
        latest = ordered[-1] if ordered else None
        return {**self.info, "turn_count": len(self.turns),
                "last_status": latest["status"] if latest else "empty",
                "context_version": len(self.context),
                "oldest_ordinal": ordered[0]["ordinal"] if ordered else None,
                "latest_ordinal": latest["ordinal"] if latest else None}


class _TranscriptView:
    """An append-only projection. The JSONL file remains the sole authority."""

    def __init__(self):
        self._reset()

    def _reset(self):
        self.sessions: OrderedDict[str, _SessionView] = OrderedDict()
        self.aliases: dict[str, str] = {}
        self.inputs: dict[str, tuple[str, str]] = {}
        self.seen: set[str] = set()
        self.offset = 0
        self.signature: tuple[int, int, int, int] | None = None
        self.anchor = b""

    def _session(self, event: dict) -> _SessionView:
        helper_id, thread_id = event.get("helper_id"), event.get("thread_id")
        if helper_id:
            key = "helper:" + str(helper_id)
        elif thread_id:
            key = "thread:" + str(thread_id)
        else:
            unbound = [item for item in self.sessions.values()
                       if item.info["thread_id"] is None and not item.info["helper_id"]]
            key = (unbound[0].info["key"] if len(unbound) == 1 else
                   "unbound:" + str(event.get("attempt_id") or event.get("input_id") or event["id"]))
        key = self.aliases.get(key, key)
        # The start notification can arrive before its input-delivered callback.
        # Adopt the sole unbound conversation without inventing another turn.
        if key not in self.sessions and thread_id and not helper_id:
            unbound = [item for item in self.sessions.values()
                       if item.info["thread_id"] is None and not item.info["helper_id"]]
            if len(unbound) == 1:
                old_key = unbound[0].info["key"]
                session = self.sessions.pop(old_key)
                session.info["key"] = key
                self.sessions[key] = session
                self.aliases[old_key] = key
                for turn in session.turns.values():
                    for input_id in turn["inputs"]:
                        self.inputs[input_id] = (key, turn["key"])
        if key not in self.sessions:
            self.sessions[key] = _SessionView(key, event)
        session = self.sessions[key]
        if thread_id:
            session.info["thread_id"] = thread_id
        kind = event.get("helper_kind") or (event.get("kind") if event.get("type") == "input" else None)
        if helper_id and kind in {"assignment", "recruitment"}:
            session.info["kind"] = kind
        session.touch(event.get("at"))
        return session

    def _move_pending(self, pending: _SessionView, target: _SessionView) -> None:
        if pending is target or target.info["helper_id"]:
            return
        for key, context in pending.context.items():
            target.context.setdefault(key, context)
        target.latest_instructions = target.latest_instructions or pending.latest_instructions
        for turn in list(pending.turns.values()):
            turn["ordinal"] = target.next_ordinal
            target.next_ordinal += 1
            target.turns[turn["key"]] = turn
            for input_id in turn["inputs"]:
                self.inputs[input_id] = (target.info["key"], turn["key"])
        target._ordered = None
        target.superseded_turn_keys.update(pending.superseded_turn_keys)
        target.touch(pending.info["started_at"])
        target.touch(pending.info["updated_at"])
        self.aliases[pending.info["key"]] = str(target.info["key"])
        del self.sessions[pending.info["key"]]

    @staticmethod
    def _touch_turn(turn: dict, event: dict) -> None:
        at = event.get("at")
        if at:
            turn["started_at"] = min(turn["started_at"] or at, at)
            turn["updated_at"] = max(turn["updated_at"] or at, at)

    def _turn(self, session: _SessionView, event: dict, *, input_key: str | None = None) -> dict:
        if input_key is None and event.get("input_id"):
            input_key = "input:" + str(event["input_id"])
        native_id = event.get("turn_id") or event.get("expected_turn_id")
        if native_id and native_id in session.native_turns:
            return session.turns[session.native_turns[native_id]]
        if input_key and input_key in session.turns:
            turn = session.turns[input_key]
        else:
            # Native events may precede the delivered input-status callback.
            # A single prepared primary input is the unambiguous binding.
            pending = [item for item in session.turns.values() if not item["turn_id"]
                       and item["status"] in {"prepared", "pending"}
                       and any(value.get("kind") != "steer" for value in item["inputs"].values())]
            if native_id and len(pending) == 1:
                turn = pending[0]
            elif not native_id and not input_key and len(session.turns) == 1:
                # Old adapters occasionally omitted turnId on visible items.
                # A sole turn is safe to associate; never guess among turns.
                turn = next(iter(session.turns.values()))
            else:
                key = input_key or ("turn:" + str(native_id) if native_id else "event:" + str(event["id"]))
                turn = session.turns.get(key) or session.new_turn(key, event)
        if native_id:
            turn["turn_id"] = native_id
            session.native_turns[str(native_id)] = turn["key"]
        return turn

    def _bind_input(self, event: dict) -> tuple[_SessionView, dict] | None:
        location = self.inputs.get(str(event.get("input_id")))
        if location is None:
            return None
        old = self.sessions[self.aliases.get(location[0], location[0])]
        turn = old.turns[location[1]]
        if event.get("helper_id") or event.get("thread_id"):
            target = self._session(event)
        else:
            target = old
            native_id = event.get("turn_id")
            matches = [item for item in self.sessions.values() if native_id in item.native_turns]
            if old.info["thread_id"] is None and len(matches) == 1:
                target = matches[0]
        if old.info["thread_id"] is None and target is not old:
            self._move_pending(old, target)
            old = target
        if target is not old:
            old.turns.pop(turn["key"])
            old._ordered = None
            turn["ordinal"] = target.next_ordinal
            target.next_ordinal += 1
            target.turns[turn["key"]] = turn
            target._ordered = None
            for input_id in turn["inputs"]:
                self.inputs[input_id] = (target.info["key"], turn["key"])
        native_id = event.get("turn_id")
        if native_id:
            other_key = target.native_turns.get(native_id)
            if other_key and other_key != turn["key"]:
                other = target.turns.pop(other_key)
                target._ordered = None
                target.superseded_turn_keys.add(other_key)
                for field in ("inputs", "replies", "actions"):
                    turn[field].update(other[field])
                if other["_native_status"]:
                    turn.update(status=other["status"], completed_at=other["completed_at"], _native_status=True)
                self._touch_turn(turn, {"at": other["started_at"]})
                self._touch_turn(turn, {"at": other["updated_at"]})
                turn["ordinal"] = min(turn["ordinal"], other["ordinal"])
                for input_id in turn["inputs"]:
                    self.inputs[input_id] = (target.info["key"], turn["key"])
            turn["turn_id"] = native_id
            target.native_turns[native_id] = turn["key"]
        return target, turn

    def apply(self, event: dict) -> None:
        event_id = event.get("id")
        if isinstance(event_id, str):
            if event_id in self.seen:
                return
            self.seen.add(event_id)
        event = {"id": event_id or "record:" + str(self.offset), **event}
        event_type = event.get("type")
        if event_type not in {"input_status", "instructions", "input", "assistant_delta", "assistant", "turn",
                              "action"}:
            return
        if event_type == "input_status":
            bound = self._bind_input(event)
            if bound is None:
                return
            session, turn = bound
            record = turn["inputs"][event["input_id"]]
            record.update({key: value for key, value in event.items()
                           if key not in {"type", "id", "at", "text"}})
            record["updated_at"] = event.get("at")
            if not turn["_native_status"]:
                turn["status"] = "in_progress" if record["status"] == "delivered" else record["status"]
        else:
            session = self._session(event)
            if event_type == "instructions":
                body = {key: value for key, value in event.items() if key not in {"id", "at"}}
                key = "context:" + _key(canonical(body))
                session.context.setdefault(key, {**event, "key": key})
                session.latest_instructions = key
                return
            if event_type == "input":
                input_id = event.get("input_id") or event["id"]
                if input_id in self.inputs:
                    return
                turn = self._turn(session, event, input_key="input:" + str(input_id))
                record = {key: value for key, value in event.items() if key not in {"id", "type", "worker_id"}}
                record.update(input_id=input_id, at=event.get("at"), updated_at=event.get("at"))
                turn["inputs"][input_id] = record
                self.inputs[input_id] = (session.info["key"], turn["key"])
                if not turn["_native_status"] and len(turn["inputs"]) == 1:
                    turn["status"] = str(event.get("status", "prepared"))
                try:
                    packet = json.loads(event.get("text", ""))
                except (TypeError, json.JSONDecodeError):
                    packet = None
                # A decision's prompt names no identifiers, so its input records the task itself.
                task = event.get("task") if isinstance(event.get("task"), dict) else (
                    packet.get("task") if isinstance(packet, dict) else None)
                if isinstance(task, dict):
                    turn["task"] = {key: task.get(key) for key in ("id", "title", "kind", "revision")}
                    contract = task.get("contract")
                    if isinstance(contract, dict):
                        for key in ("title", "kind"):
                            turn["task"][key] = turn["task"][key] or contract.get(key)
            else:
                turn = self._turn(session, event)
                if event_type in {"assistant", "assistant_delta"}:
                    item_id = event.get("item_id") or event["id"]
                    key = "reply:" + str(item_id)
                    reply = turn["replies"].setdefault(key, {"key": key, "item_id": item_id, "text": "",
                        "phase": None, "status": "streaming", "started_at": event.get("at"), "updated_at": event.get("at")})
                    if event_type == "assistant":
                        reply.update(text=event.get("text", ""), phase=event.get("phase"), status="completed")
                    elif reply["status"] != "completed":
                        reply["text"] += event.get("text", "")
                    reply["updated_at"] = event.get("at")
                elif event_type == "turn":
                    status = _status(event.get("status"))
                    if turn["status"] not in _TERMINAL_TURNS or status in _TERMINAL_TURNS:
                        turn.update(status=status, _native_status=True)
                        if status in _TERMINAL_TURNS:
                            turn["completed_at"] = event.get("at")
                        if status in _TERMINAL_TURNS or status == "uncertain":
                            for reply in turn["replies"].values():
                                if reply["status"] == "streaming":
                                    reply["status"] = "incomplete"
                elif event_type == "action":
                    key = "action:" + str(event.get("action_id") or event["id"])
                    action = turn["actions"].setdefault(key, {"key": key, "started_at": event.get("at")})
                    action.update({name: value for name, value in event.items()
                                   if name not in {"type", "id", "at", "worker_id", "thread_id", "turn_id", "helper_id"}})
                    action["updated_at"] = event.get("at")
        session.touch(event.get("at"))
        self._touch_turn(turn, event)

    def read_append(self, path: Path) -> None:
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if signature == self.signature:
            return
        with path.open("rb") as stream:
            reset = self.signature is not None and (
                signature[:2] != self.signature[:2] or stat.st_size < self.offset
                or (stat.st_size == self.signature[2] and stat.st_mtime_ns != self.signature[3]))
            if self.offset and not reset:
                stream.seek(max(0, self.offset - len(self.anchor)))
                reset = stream.read(len(self.anchor)) != self.anchor
            if reset:
                self._reset()
            stream.seek(self.offset)
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    stream.seek(start)
                    break
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise HubError("TRANSCRIPT_INVALID", "Transcript events must be JSON objects")
                self.apply(event)
                self.offset = stream.tell()
            stream.seek(max(0, self.offset - 256))
            self.anchor = stream.read(self.offset - stream.tell())
        self.signature = signature


# Directed messages reach a model as file references; these are their only homes.
_MESSAGE_ROOTS = (("correspondence",), (".task-based", "private", "peer-talk"))
_MESSAGE_BYTES = 65536
_CORRESPONDENCE_HEADER = re.compile(r"\A\s*```json\r?\n.*?\r?\n```(?:\r?\n)+", re.S)


def _message_body(project: Path, reference: Any) -> str | None:
    if not isinstance(reference, str) or not reference:
        return None
    try:
        path = project.joinpath(reference).resolve()  # A relative reference names a project file.
        if not any(path.is_relative_to(project.joinpath(*parts).resolve()) for parts in _MESSAGE_ROOTS):
            return None
        with path.open("rb") as stream:
            text = stream.read(_MESSAGE_BYTES).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    if path.suffix == ".json":
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value.get("text") if isinstance(value, dict) and isinstance(value.get("text"), str) else None
    # Human correspondence starts with a fenced JSON header, then the message body.
    header = _CORRESPONDENCE_HEADER.match(text)
    return text[header.end():] if header else text


def inline_message_bodies(page: dict, project: Path) -> dict:
    """Give an operator's view the bodies of the directed messages a turn received.

    Model inputs carry only file references. Only human correspondence and peer
    talk files are read; any other reference keeps its path and has no text.
    """
    project = Path(project).resolve()
    for turn in page.get("turns", []):
        for record in turn.get("inputs", []):
            try:
                packet = json.loads(record.get("text") or "")
            except (TypeError, ValueError):
                continue
            if not isinstance(packet, dict):
                continue
            groups = [packet.get("messages")] + [
                value.get("messages") for value in (packet.get("human_messages"), packet.get("private_talk"))
                if isinstance(value, dict)]
            references = [item for group in groups if isinstance(group, list) for item in group if isinstance(item, dict)]
            if not references:
                continue
            record["messages"] = []
            for item in references:
                path = item.get("file", {}).get("path") if isinstance(item.get("file"), dict) else None
                text = item.get("text") if isinstance(item.get("text"), str) else _message_body(project, path)
                record["messages"].append({"sender": item.get("sender"), "sender_role": item.get("sender_role"),
                                           "path": path, "text": text})
    return page


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_lines(directory: Path, lines: list[bytes]) -> None:
    """Append complete records to one worker's events file, then flush and sync it once."""
    directory.mkdir(parents=True, exist_ok=True)
    # The file lock covers other processes; _ActivityWriter orders the threads of this one.
    with portalocker.Lock(str(directory / ".lock"), timeout=10):
        with (directory / "events.jsonl").open("a+b") as stream:
            end = stream.seek(0, 2)
            if end:
                stream.seek(end - 1)
                if stream.read(1) != b"\n":
                    boundary, position = 0, end
                    while position:
                        start = max(0, position - 65536)
                        stream.seek(start)
                        block = stream.read(position - start)
                        newline = block.rfind(b"\n")
                        if newline >= 0:
                            boundary = start + newline + 1
                            break
                        position = start
                    stream.seek(boundary)
                    # Keep a torn tail for diagnostics, but never join it to
                    # the next JSON record or expose it as completed input.
                    tail = directory / ("torn-tail-" + uuid4().hex + ".bin")
                    with tail.open("xb") as saved:
                        while block := stream.read(65536):
                            saved.write(block)
                        saved.flush()
                        os.fsync(saved.fileno())
                    stream.truncate(boundary)
            stream.seek(0, 2)
            stream.write(b"".join(lines))
            stream.flush()
            os.fsync(stream.fileno())


class _ActivityWriter:
    """Writes visible activity off the event loop. A worker's records reach its file in the
    order they were submitted and before any record written synchronously after them; what
    has accumulated for one file is synced once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[Path, list[bytes]] = {}
        self._files: dict[Path, threading.Lock] = {}
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def submit(self, directory: Path, line: bytes) -> None:
        with self._lock:
            self._pending.setdefault(directory, []).append(line)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="transcript-writer", daemon=True)
                self._thread.start()
        self._wake.set()

    def write(self, directory: Path, lines: list[bytes] | None = None, *, logged: bool = False) -> None:
        """Write this file's pending activity, then ``lines``, before returning. A failure is
        raised, or with ``logged`` only logged."""
        with self._lock:
            file_lock = self._files.setdefault(directory, threading.Lock())
        with file_lock:
            with self._lock:
                batch = self._pending.pop(directory, []) + (lines or [])
            if not batch:
                return
            try:
                _write_lines(directory, batch)
            except Exception:
                if not logged:
                    raise
                # Visible history only; the coordination ledger is unaffected.
                logger.exception("Transcript activity could not be written to %s", directory)

    def flush(self) -> None:
        """Return once everything submitted before the call is written, or has failed."""
        with self._lock:
            directories = set(self._files) | set(self._pending)
        for directory in directories:
            self.write(directory, logged=True)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                directories = list(self._pending)
            for directory in directories:
                self.write(directory, logged=True)


_WRITER = _ActivityWriter()
# Reply text streamed but not yet written: {worker directory: {item id: stream}}.
_STREAMS: dict[Path, dict[str, dict]] = {}
_STREAMS_LOCK = threading.Lock()


def _encode(worker_id: str, event: dict, at: str | None = None) -> tuple[str, bytes]:
    record = {"id": uuid4().hex, "at": at or datetime.now(UTC).isoformat(), "worker_id": worker_id, **event}
    return str(record["id"]), (canonical(record) + "\n").encode("utf-8")


def _release_streams(directory: Path, item_id: str | None = None, *, due_only: bool = False,
                     forget: bool = False) -> None:
    """Queue the reply text collected for one worker, or for one of its items, ahead of
    whatever is written next; ``due_only`` keeps text younger than the interval."""
    now = _clock()
    with _STREAMS_LOCK:  # Held while queueing, so the chunks of one reply keep their order.
        streams = _STREAMS.get(directory, {})
        for key in list(streams) if item_id is None else [item_id]:
            stream = streams.get(key)
            if stream is None:
                continue
            if stream["parts"] and not (due_only and now - stream["since"] < DELTA_FLUSH_SECONDS):
                _, line = _encode(stream["worker_id"], {**stream["event"], "text": "".join(stream["parts"])},
                                  stream["at"])
                stream["parts"].clear()
                _WRITER.submit(directory, line)
            if forget:
                del streams[key]
        if not streams:
            _STREAMS.pop(directory, None)


def _flush_all() -> None:
    """Write everything this process has collected or queued for any worker."""
    with _STREAMS_LOCK:
        directories = list(_STREAMS)
    for directory in directories:
        _release_streams(directory)
    _WRITER.flush()


atexit.register(_flush_all)


class TranscriptStore:
    def __init__(self, root: Path):
        self.root = Path(root) / ".task-based" / "transcripts"

    def _directory(self, worker_id: str) -> Path:
        return self.root / "workers" / _key(worker_id)

    def _append(self, worker_id: str, event: dict) -> str:
        """Write and sync one record before returning, after everything captured before it
        for the same worker."""
        directory = self._directory(worker_id)
        _release_streams(directory)
        record_id, line = _encode(worker_id, event)
        _WRITER.write(directory, [line])
        return record_id

    def _queue(self, worker_id: str, event: dict) -> str:
        """Visible activity: written in order by the background writer."""
        directory = self._directory(worker_id)
        _release_streams(directory)
        record_id, line = _encode(worker_id, event)
        _WRITER.submit(directory, line)
        return record_id

    def events_path(self, worker_id: str) -> Path:
        """The worker's append-only event file, after everything queued for it is written."""
        self._settle(worker_id)
        return self._directory(worker_id) / "events.jsonl"

    def _settle(self, worker_id: str) -> None:
        """Write what this process has collected or queued for a worker before reading it."""
        directory = self._directory(worker_id)
        _release_streams(directory)
        _WRITER.write(directory)

    def flush(self) -> None:
        """Write everything this process has collected or queued for any worker."""
        _flush_all()

    def prepare_input(self, worker_id: str, thread_id: str | None, source: str,
                      text: str, attempt_id: str | None = None,
                      helper_id: str | None = None, kind: str = "input", **metadata) -> str:
        input_id = uuid4().hex
        record = {"input_id": input_id, "worker_id": worker_id, "thread_id": thread_id,
                  "source": source, "text": text, "attempt_id": attempt_id,
                  "helper_id": helper_id, "kind": kind, "status": "prepared", **metadata}
        atomic_json(self.root / "inputs" / (input_id + ".json"), record)
        self._append(worker_id, {"type": "input", **record})
        return input_id

    def _input(self, input_id: str) -> tuple[Path, dict]:
        if len(input_id) != 32 or any(c not in "0123456789abcdef" for c in input_id):
            raise HubError("INVALID_INPUT", "Invalid transcript input ID", 422)
        path = self.root / "inputs" / (input_id + ".json")
        if not path.is_file():
            raise HubError("NOT_FOUND", "Transcript input does not exist", 404)
        return path, json.loads(path.read_text(encoding="utf-8"))

    def delivered(self, input_id: str, turn_id: str | None = None, **metadata) -> None:
        path, record = self._input(input_id)
        if (record["status"] == "delivered" and record.get("turn_id") == turn_id
                and all(record.get(key) == value for key, value in metadata.items())):
            return
        record.update(status="delivered", turn_id=turn_id, **metadata)
        self._append(record["worker_id"], {"type": "input_status", "input_id": input_id,
                     "thread_id": record.get("thread_id"), "helper_id": record.get("helper_id"),
                     "status": "delivered", "turn_id": turn_id, **metadata})
        atomic_json(path, record)

    def ambiguous(self, input_id: str, error: Any) -> None:
        path, record = self._input(input_id)
        if record["status"] == "delivered":
            return
        record.update(status="uncertain", error=str(error))
        self._append(record["worker_id"], {"type": "input_status", "input_id": input_id,
                     "thread_id": record.get("thread_id"), "helper_id": record.get("helper_id"),
                     "status": "uncertain", "error": str(error)})
        atomic_json(path, record)

    def reconciled(self, input_id: str, *, delivered: bool, turn_id: str | None,
                   reason: str) -> None:
        """Append a known transport resolution without replaying the input."""
        if delivered:
            self.delivered(input_id, turn_id, reconciliation=reason)
            return
        path, record = self._input(input_id)
        if record["status"] == "not_delivered":
            return
        if record["status"] == "delivered":
            raise HubError("DELIVERY_CONFLICT", "A confirmed delivered input cannot become not_delivered")
        record.update(status="not_delivered", turn_id=turn_id, reconciliation=reason)
        self._append(record["worker_id"], {"type": "input_status", "input_id": input_id,
                     "thread_id": record.get("thread_id"), "helper_id": record.get("helper_id"),
                     "status": "not_delivered", "turn_id": turn_id, "reconciliation": reason})
        atomic_json(path, record)

    def record_instructions(self, worker_id: str, thread_id: str | None,
                            instructions: dict | str, **metadata) -> None:
        self._append(worker_id, {"type": "instructions", "thread_id": thread_id,
                                "instructions": instructions, **metadata})

    def record_action(self, worker_id: str, thread_id: str | None, turn_id: str | None,
                      action_id: str, tool: str, status: str, **metadata) -> None:
        """Store visible action metadata; callers omit raw streams and internal reasoning."""
        self._queue(worker_id, {"type": "action", "thread_id": thread_id, "turn_id": turn_id,
                               "action_id": action_id, "tool": tool, "status": status, **metadata})

    def record_uncertain_turn(self, worker_id: str, thread_id: str | None, turn_id: str | None,
                              input_id: str, **metadata) -> None:
        """A stopped/failed transport is not proof of a native terminal outcome."""
        self._append(worker_id, {"type": "turn", "thread_id": thread_id, "turn_id": turn_id,
                                "input_id": input_id, "status": "uncertain",
                                "source": "runtime_observation", "terminal_observed": False, **metadata})

    def capture_native(self, thread_id: str | None, worker_id: str, method: str,
                       params: dict, **metadata) -> None:
        directory = self._directory(worker_id)
        if method == "item/agentMessage/delta":
            # Collected into timed chunks rather than one synced record per delta.
            item_id = str(params.get("itemId"))
            with _STREAMS_LOCK:
                stream = _STREAMS.setdefault(directory, {}).setdefault(item_id, {
                    "worker_id": worker_id, "parts": [], "since": 0.0, "at": None,
                    "event": {"thread_id": thread_id, "type": "assistant_delta", "item_id": params.get("itemId"),
                              "turn_id": params.get("turnId"), **metadata}})
                if not stream["parts"]:  # A chunk is dated by its first delta.
                    stream.update(since=_clock(), at=datetime.now(UTC).isoformat())
                stream["parts"].append(params.get("delta", ""))
            _release_streams(directory, item_id, due_only=True)
            return
        if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            item = params["item"]
            # The completed reply replaces the text streamed for it.
            with _STREAMS_LOCK:
                _STREAMS.get(directory, {}).pop(str(item.get("id")), None)
            event = {"type": "assistant", "item_id": item.get("id"),
                     "text": item.get("text", ""), "phase": item.get("phase"),
                     "turn_id": params.get("turnId")}
        elif method in {"turn/started", "turn/completed"}:
            # A reply its turn ended without completing keeps the text streamed for it.
            _release_streams(directory, forget=True)
            turn = params.get("turn", {})
            event = {"type": "turn", "turn_id": turn.get("id"),
                     "status": turn.get("status") or ("inProgress" if method == "turn/started" else "unknown"), "method": method}
        elif method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            kind = item.get("type")
            if kind not in {"commandExecution", "fileChange", "webSearch", "mcpToolCall"}:
                return
            # These are visible tool actions, not stdout, diffs, telemetry, or
            # reasoning. Framework dynamic tools have a separate exact hook.
            details = {key: item[key] for key in ("command", "cwd", "exitCode", "server", "tool")
                       if key in item and isinstance(item[key], (str, int, type(None)))}
            if kind == "fileChange":
                details["paths"] = [change["path"] for change in item.get("changes", [])
                                    if isinstance(change, dict) and isinstance(change.get("path"), str)]
            if kind == "webSearch" and isinstance(item.get("action"), dict):
                details.update({key: item["action"][key] for key in ("query", "url", "pattern")
                                if isinstance(item["action"].get(key), str)})
            event = {"type": "action", "action_id": item.get("id"), "tool": kind,
                     "turn_id": params.get("turnId"), "details": details,
                     "status": item.get("status") or ("completed" if method == "item/completed" else "running")}
        else:
            return
        self._queue(worker_id, {"thread_id": thread_id, **event, **metadata})

    def _view(self, worker_id: str) -> _TranscriptView:
        """Called under the shared projection lock; reading never creates files."""
        path = self.root / "workers" / _key(worker_id) / "events.jsonl"
        cache_key = str(path.resolve())
        view = _VIEWS.get(cache_key)
        if view is None:
            view = _TranscriptView()
            _VIEWS[cache_key] = view
        _VIEWS.move_to_end(cache_key)
        while len(_VIEWS) > _VIEW_CACHE_SIZE:
            _VIEWS.popitem(last=False)
        try:
            view.read_append(path)
        except FileNotFoundError:
            view._reset()
        except (ValueError, TypeError, KeyError) as exc:
            # A malformed complete line is not an excuse to drop visible history.
            # The raw byte endpoint remains available for inspection.
            _VIEWS.pop(cache_key, None)
            raise HubError("TRANSCRIPT_INVALID", "Stored transcript contains an invalid event", 409) from exc
        return view

    def sessions(self, worker_id: str) -> dict:
        """Return retained main/helper conversations without loading a native session."""
        self._settle(worker_id)
        with _VIEW_LOCK:
            view = self._view(worker_id)
            sessions = [session.metadata() for session in view.sessions.values()]
            sessions.sort(key=lambda item: (item["updated_at"] or "", item["key"]), reverse=True)
            return {"sessions": deepcopy(sessions)}

    def session_page(self, worker_id: str, session_key: str, *, before: int | None = None,
                     limit: int = 20, include_context: bool = True) -> dict:
        """Newest turns by default; ``before`` is an exclusive ordinal cursor.

        Ordinals normally increase consecutively. A late transport binding can
        merge an orphan native event with its prepared input, leaving a gap;
        clients use has_more/next_before rather than arithmetic gap detection.
        """
        if (before is not None and (type(before) is not int or before < 1)
                or type(limit) is not int or not 1 <= limit <= 100
                or type(include_context) is not bool):
            raise HubError("INVALID_INPUT", "Invalid transcript turn cursor or page size", 422)
        self._settle(worker_id)
        with _VIEW_LOCK:
            view = self._view(worker_id)
            session = view.sessions.get(view.aliases.get(session_key, session_key))
            if session is None:
                raise HubError("NOT_FOUND", "Transcript session does not exist", 404)
            ordered = session.ordered_turns()
            end = len(ordered) if before is None else bisect_left(ordered, before, key=lambda turn: turn["ordinal"])
            start = max(0, end - limit)
            page = ordered[start:end]
            turns = []
            for turn in page:
                record = {key: value for key, value in turn.items() if not key.startswith("_")}
                for field in ("inputs", "replies", "actions"):
                    record[field] = list(turn[field].values())
                turns.append(record)
            has_more = start > 0
            return deepcopy({"session": session.metadata(), "turns": turns, "has_more": has_more,
                             "next_before": page[0]["ordinal"] if has_more else None,
                             "superseded_turn_keys": sorted(session.superseded_turn_keys),
                             "context": list(session.context.values()) if include_context else []})

    def page(self, worker_id: str, *, cursor: int = 0, limit: int = 100) -> dict:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise HubError("INVALID_INPUT", "Invalid transcript cursor or page size", 422)
        self._settle(worker_id)
        path = self.root / "workers" / _key(worker_id) / "events.jsonl"
        if not path.exists():
            if cursor:
                raise HubError("INVALID_INPUT", "Transcript cursor is outside the file", 422)
            return {"events": [], "next_cursor": 0, "has_more": False}
        events = []
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            if cursor > size:
                raise HubError("INVALID_INPUT", "Transcript cursor is outside the file", 422)
            if cursor:
                stream.seek(cursor - 1)
                if stream.read(1) != b"\n":
                    raise HubError("INVALID_INPUT", "Transcript cursor must identify a record boundary", 422)
            stream.seek(cursor)
            for _ in range(limit):
                start = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    stream.seek(start)
                    break
                events.append(json.loads(line))
            next_cursor = stream.tell()
        return {"events": events, "next_cursor": next_cursor, "has_more": next_cursor < size}
