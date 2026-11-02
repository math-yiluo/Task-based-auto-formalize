"""What a worker's recorded history says about its recent work, and keyword search over it.

Both read the worker's own append-only transcript (`events.jsonl`). They never run on the
coordinator's database thread: callers use a worker thread.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PATH_ARGUMENTS = ("path", "source_path", "directory", "paths", "entry_files", "files")
WRITING_OPERATIONS = {"workspace_write", "workspace_copy", "stage", "contribute", "draft_update",
                      "dag_update", "wiki_submit"}
_PATH_TOKEN = re.compile(r"""(?:[A-Za-z]:)?[\w.\-]+(?:[/\\][\w.\-]+)+""")
_EXCERPT = 240


def events(path: Path) -> Iterator[dict[str, Any]]:
    """The recorded events in order; a line that cannot be read is skipped, not fatal."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _argument_paths(arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    found: list[str] = []
    for key in PATH_ARGUMENTS:
        value = arguments.get(key)
        values = value if isinstance(value, list) else [value]
        found += [item for item in values if isinstance(item, str) and item.strip()]
    nested = arguments.get("arguments")
    return found + (_argument_paths(nested) if isinstance(nested, dict) else [])


def _command_paths(command: Any) -> list[str]:
    if not isinstance(command, str):
        return []
    return [token for token in _PATH_TOKEN.findall(command)
            if "." in re.split(r"[/\\]", token)[-1]]


def digest(path: Path, thread_id: str | None, *, files: int = 40, replies: int = 3,
           errors: int = 10, reply_characters: int = 800) -> dict[str, Any]:
    """The files a thread recently touched, its last visible replies and its recent errors."""
    touched: dict[str, dict[str, Any]] = {}
    commentary: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for event in events(path):
        if thread_id is not None and event.get("thread_id") != thread_id:
            continue
        kind, at = event.get("type"), event.get("at")
        if kind == "assistant" and isinstance(event.get("text"), str) and event["text"].strip():
            text = event["text"].strip()
            commentary.append({"at": at, "text": text if len(text) <= reply_characters
                               else text[:reply_characters] + " [truncated]"})
            del commentary[:-replies]
        if kind != "action":
            continue
        details, arguments = _mapping(event.get("details")), _mapping(event.get("arguments"))
        tool = event.get("tool")
        operation = event.get("operation") or arguments.get("operation")
        if tool == "fileChange":
            for item in details.get("paths", []):
                touched.pop(item, None)
                touched[item] = {"path": item, "use": "edited", "at": at}
        elif tool == "commandExecution" and event.get("status") in {"running", "inProgress"}:
            for item in _command_paths(details.get("command")):
                if touched.get(item, {}).get("use") != "edited":
                    touched.pop(item, None)
                    touched[item] = {"path": item, "use": "used in a command", "at": at}
        elif event.get("status") == "running":
            use = "written" if operation in WRITING_OPERATIONS else "read"
            for item in _argument_paths(arguments):
                previous = touched.pop(item, None)
                touched[item] = {"path": item, "at": at,
                                 "use": "edited" if previous and previous["use"] == "edited" else use}
        result = _mapping(event.get("result"))
        if event.get("status") == "error" or result.get("error") or result.get("code"):
            failures.append({"at": at, "tool": tool, "operation": operation,
                             "code": result.get("code") or event.get("error_type"),
                             "error": str(result.get("error") or "")[:300]})
            del failures[:-errors]
        elif tool == "commandExecution" and isinstance(details.get("exitCode"), int) and details["exitCode"] != 0:
            failures.append({"at": at, "tool": tool, "command": str(details.get("command", ""))[:300],
                             "exit_code": details["exitCode"]})
            del failures[:-errors]
    recent = list(touched.values())[-files:]
    recent.reverse()
    return {"files": recent, "replies": commentary, "errors": failures}


def _texts(event: dict[str, Any]) -> list[str]:
    kind = event.get("type")
    if kind in {"assistant", "input"} and isinstance(event.get("text"), str):
        return [event["text"]]
    if kind == "action":
        texts = [json.dumps(event.get("arguments"), ensure_ascii=False)] if event.get("arguments") else []
        details = event.get("details")
        if isinstance(details, dict) and isinstance(details.get("command"), str):
            texts.append(details["command"])
        return texts
    return []


def search(path: Path, query: str, *, limit: int = 10, exclude_thread: str | None = None) -> dict[str, Any]:
    """Case-insensitive matches of every word of `query`, newest first, with short excerpts."""
    words = [word.casefold() for word in query.split() if word.strip()]
    if not words:
        return {"query": query, "matches": [], "truncated": False}
    matches: list[dict[str, Any]] = []
    for event in events(path):
        if exclude_thread is not None and event.get("thread_id") == exclude_thread:
            continue
        for text in _texts(event):
            folded = text.casefold()
            if not all(word in folded for word in words):
                continue
            at = folded.find(words[0])
            start = max(0, at - _EXCERPT // 2)
            excerpt = text[start:start + _EXCERPT]
            matches.append({"at": event.get("at"), "conversation": event.get("thread_id"),
                            "kind": "message to you" if event.get("type") == "input" else
                                    "your reply" if event.get("type") == "assistant" else "your action",
                            "excerpt": ("…" if start else "") + excerpt + ("…" if start + _EXCERPT < len(text) else "")})
            break
    matches.reverse()
    return {"query": query, "matches": matches[:limit], "truncated": len(matches) > limit}
