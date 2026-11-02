"""A reviewer's verdict as a file: later questions about the review start from it.

The file is written when the reviewer gives its verdict and recorded with the review; a
clarification turn then needs neither the reviewer's old conversation nor its context.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..content import PRIVATE_DIRECTORY
from .files import ScopedFiles

SECTIONS = (("rationale", "Rationale"), ("evidence", "Evidence"), ("semantic_checks", "Semantic checks"),
            ("qualifications", "Qualifications"), ("suggestions", "Suggestions"),
            ("integration_notes", "Integration notes"))


def report_path(review_task_id: str) -> str:
    return f"{PRIVATE_DIRECTORY}/reviews/{review_task_id}/report.md"


def _render_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip() or "(empty)"]
    if isinstance(value, list):
        return [("- " + item.strip()) if isinstance(item, str)
                else "- " + json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value] or ["(none)"]
    return ["```json", json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), "```"]


def render(*, review_task_id: str, candidate_id: str | None, verdict: str, fields: dict[str, Any],
           files: str | None = None) -> str:
    lines = [f"# Review report: candidate `{candidate_id or 'unknown'}`", "",
             f"- Verdict: {verdict}", f"- Review task: `{review_task_id}`",
             *([f"- Candidate files: `{files}`"] if files else []), ""]
    for key, title in SECTIONS:
        if fields.get(key) not in (None, "", [], {}):
            lines += [f"## {title}", "", *_render_value(fields[key]), ""]
    return "\n".join(lines).rstrip() + "\n"


def write(workspace: Path, review_task_id: str, text: str) -> tuple[str, str]:
    """Write the report inside the reviewer's private area; return its path and digest."""
    relative = report_path(review_task_id)
    ScopedFiles(workspace).write(relative, text)
    return relative, hashlib.sha256(text.encode("utf-8")).hexdigest()
