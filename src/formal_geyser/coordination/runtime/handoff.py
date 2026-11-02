"""Handoff files: what a new conversation needs to continue the same task.

A conversation can be replaced at any time, so an agent keeps its working memory in
files: one handoff file per task and an append-only journal. Before a rotation the
runtime checks the handoff file; when the agent cannot supply a valid one, the new
conversation starts from the runtime's own state packet instead.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..content import PRIVATE_DIRECTORY

TASK_DIRECTORY = f"{PRIVATE_DIRECTORY}/tasks"
JOURNAL_PATH = f"{PRIVATE_DIRECTORY}/notes/journal.md"
HANDOFF_SECTIONS = ("Goal", "Current state", "Next step", "Remaining plan", "Key decisions",
                    "Dead ends", "Key facts", "Open questions")
REQUIRED_SECTIONS = ("Goal", "Current state", "Next step")
# A review's handoff: what is checked, what was found and what remains.
REVIEW_SECTIONS = ("Goal", "Checked so far", "Findings", "Next step", "Open questions")
REVIEW_REQUIRED = ("Goal", "Checked so far", "Next step")
HANDOFF_MAX_CHARACTERS = 12_000  # About 3,000 tokens.
MISSING_FILES_REPORTED = 5
_PLACEHOLDERS = {"", "-", "*", "...", "…", "todo", "tbd", "n/a"}
_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.M)
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def handoff_path(task_id: str) -> str:
    """The workspace-relative handoff file of one task."""
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", task_id):
        raise ValueError("A handoff file needs a task identifier")
    return f"{TASK_DIRECTORY}/{task_id}/handoff.md"


def handoff_template(title: str, contract_path: str | None = None, *, review: bool = False) -> str:
    """The fixed sections, ready to fill in: a task's, or a review's."""
    goal = title.strip() or "The task's objective in one sentence."
    lines = ["# Handoff", "", "## Goal", "", goal + (f" Contract: `{contract_path}`." if contract_path else ""), ""]
    for section in (REVIEW_SECTIONS if review else HANDOFF_SECTIONS)[1:]:
        lines += [f"## {section}", "", ""]
    return "\n".join(lines)


@dataclass(frozen=True)
class HandoffCheck:
    path: str
    exists: bool
    sha256: str | None
    characters: int
    problems: tuple[str, ...]
    # Remarks that do not make the file unusable, such as files it names that are not in
    # the workspace (planned files, Mathlib or literature paths).
    notes: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.exists and not self.problems

    def record(self) -> dict:
        return {"path": self.path, "exists": self.exists, "sha256": self.sha256,
                "characters": self.characters, "valid": self.valid, "problems": list(self.problems),
                "notes": list(self.notes)}


def sections(text: str) -> dict[str, str]:
    """Each level-two heading's text, keyed by the heading in lower case."""
    found: dict[str, str] = {}
    headings = list(_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        found.setdefault(heading.group(1).strip().casefold(), text[heading.end():end].strip())
    return found


def _filled(body: str) -> bool:
    lines = [line.strip().lstrip("-*").strip().casefold() for line in body.splitlines()]
    return any(line not in _PLACEHOLDERS for line in lines)


def referenced_paths(text: str) -> list[str]:
    """Code spans and link targets that name a file or directory path.

    Commands, Lean names and prose contain spaces or no path separator, so they are not
    taken for paths.
    """
    paths = []
    for candidate in [*_CODE.findall(text), *_LINK.findall(text)]:
        candidate = candidate.strip()
        if (not candidate or " " in candidate or "://" in candidate or candidate.startswith(("#", "-"))
                or not re.search(r"[/\\]", candidate) or any(mark in candidate for mark in "*?<>|\"")):
            continue
        name = re.split(r"[/\\]", candidate.rstrip("/\\"))[-1]
        if candidate.endswith(("/", "\\")) or "." in name:
            paths.append(candidate.split("#", 1)[0])
    return list(dict.fromkeys(paths))


def _exists(workspace: Path, reference: str) -> bool:
    candidate = Path(reference)
    target = candidate if candidate.is_absolute() else workspace / candidate
    try:
        return target.exists()
    except (OSError, ValueError):
        return False


def check_handoff(workspace: Path, task_id: str, *, previous_sha256: str | None = None,
                  review: bool = False) -> HandoffCheck:
    """Whether the task's handoff file can start a new conversation.

    It must exist, fill in the required sections (a review's, for a reviewer), stay within
    the size limit and, when the context passed the limit, have changed since. Files it
    names that are not in the workspace are only noted.
    """
    relative = handoff_path(task_id)
    target = workspace / relative
    try:
        body = target.read_bytes()
    except FileNotFoundError:
        return HandoffCheck(relative, False, None, 0, ("The handoff file does not exist",))
    except OSError as error:
        return HandoffCheck(relative, False, None, 0, (f"The handoff file cannot be read: {type(error).__name__}",))
    sha256 = hashlib.sha256(body).hexdigest()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return HandoffCheck(relative, True, sha256, 0, ("The handoff file is not UTF-8 text",))
    problems = []
    if previous_sha256 is not None and sha256 == previous_sha256:
        problems.append("The handoff file was not updated after the context passed the limit")
    found = sections(text)
    for section in (REVIEW_REQUIRED if review else REQUIRED_SECTIONS):
        if section.casefold() not in found:
            problems.append(f"Section '{section}' is missing")
        elif not _filled(found[section.casefold()]):
            problems.append(f"Section '{section}' is empty")
    if len(text) > HANDOFF_MAX_CHARACTERS:
        problems.append(f"The handoff file has {len(text)} characters; the limit is {HANDOFF_MAX_CHARACTERS}")
    missing = [path for path in referenced_paths(text) if not _exists(workspace, path)]
    notes = [f"it names a file not in the workspace: {path}" for path in missing[:MISSING_FILES_REPORTED]]
    if len(missing) > MISSING_FILES_REPORTED:
        notes.append(f"it names {len(missing) - MISSING_FILES_REPORTED} more such files")
    return HandoffCheck(relative, True, sha256, len(text), tuple(problems), tuple(notes))


def handoff_sha256(workspace: Path, task_id: str) -> str | None:
    """The current file's digest, recorded when the runtime asks for an update."""
    try:
        return hashlib.sha256((workspace / handoff_path(task_id)).read_bytes()).hexdigest()
    except OSError:
        return None
