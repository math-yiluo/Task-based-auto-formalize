"""Standing instructions and the guides behind them.

The required guides are part of every activation's standing instructions, which are the
same for every worker in the same role: they lead each conversation, where they can be a
shared cached prefix, and compaction never removes them. On-demand guides stay ordinary
workspace files read when their subject comes up. File hashes identify the supplied
version, not proof that a model read it; there is no reading ledger.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..content import DEVICE_NAMES, PRIVATE_DIRECTORY
from .entries import MESSAGE_MODES
from .files import ScopedFiles

GUIDE_DIRECTORY = f"{PRIVATE_DIRECTORY}/instructions"
READING_RULE = ("The required guides are part of your standing instructions. Read an on-demand "
                "guide in reading.on_demand when its subject comes up.")


def guide_mode(*, review: bool, worker_kind: str, execution_mode: str) -> str:
    """The mode the guides serve: a correspondence turn's own, else integration, review or work."""
    if execution_mode in MESSAGE_MODES:
        return execution_mode
    return "integration" if worker_kind == "integrator" else "review" if review else "work"


def is_on_demand(name: str) -> bool:
    """Operation references and the DAG guide are read when needed; the others are required."""
    return name.endswith("-operations.md") or name == "shared-dags.md"


def guide_texts(plugin: Any, *, review: bool, worker_kind: str = "ordinary", execution_mode: str = "work") -> dict[str, str]:
    """Load source Markdown on demand so edits take effect without a restart."""
    directory = Path(__file__).with_name("guides")
    if worker_kind == "integrator":
        integration_names = (("integrator-answer.md", "integrator-operations.md") if execution_mode == "message_only" else ("integrator.md", "integrator-operations.md"))
        return {name: (directory / name).read_text(encoding="utf-8") for name in integration_names}
    if review and execution_mode == "message_only":
        return {"review-answer.md": (directory / "review-answer.md").read_text(encoding="utf-8")}
    names: tuple[str, ...] = ("worker.md", "shared-dags.md", "worker-operations.md")
    if review:
        names += ("review.md", "review-operations.md")
    result = {name: (directory / name).read_text(encoding="utf-8") for name in names}
    domain: Any = getattr(plugin, "worker_guides", lambda **_: {})(review=review)
    if not isinstance(domain, dict):
        raise ValueError("Domain worker guides must map Markdown filenames to text")
    for name, text in domain.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*\.md", name)
                or DEVICE_NAMES.fullmatch(name) or name in result or name in {"worker.md", "review.md"}
                or not isinstance(text, str) or not text.strip()):
            raise ValueError("Domain worker guides need unique safe Markdown names and nonempty text")
        result[name] = text
    return result


def prepare_guidance(workspace: Path, plugin: Any, *, review: bool,
                     context_epoch: int, compacted: bool = False,
                     execution_mode: str = "work", worker_kind: str = "ordinary") -> dict[str, Any]:
    """Refresh runtime-owned copies reachable by native and scoped file tools.

    Only this reserved private directory is replaced; user AGENTS.md files,
    notes, Library, and source work are left intact. Private paths are excluded
    from submission snapshots and public exports by the existing boundaries.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    files = ScopedFiles(workspace)
    versions: dict[str, str] = {}
    optional_versions: dict[str, str] = {}
    for name, text in guide_texts(plugin, review=review, worker_kind=worker_kind, execution_mode=execution_mode).items():
        relative = f"{GUIDE_DIRECTORY}/{name}"
        target = files.path(relative, must_exist=False)
        body = text.encode("utf-8")
        if not target.is_file() or target.read_bytes() != body:
            files.write(relative, text)
        target_versions = optional_versions if is_on_demand(name) else versions
        target_versions[name] = hashlib.sha256(body).hexdigest()
    result: dict[str, Any] = {
        "mode": guide_mode(review=review, worker_kind=worker_kind, execution_mode=execution_mode),
        "directory": GUIDE_DIRECTORY,
        "files": versions, "optional_files": optional_versions,
        "context_epoch": context_epoch, "instruction": READING_RULE,
    }
    if compacted:
        result["reason"] = "context_compacted"
    return result


def standing_guides(plugin: Any, *, review: bool, worker_kind: str = "ordinary",
                    execution_mode: str = "work") -> str:
    """The required guides' text, for the standing instructions."""
    texts = guide_texts(plugin, review=review, worker_kind=worker_kind, execution_mode=execution_mode)
    return "\n\n".join(text.strip() for name, text in texts.items() if not is_on_demand(name))


def standing_anchor(plugin: Any, *, review: bool, worker_kind: str = "ordinary",
                    execution_mode: str = "work") -> str:
    """The anchor for the standing instructions, from the guides alone (nothing is written)."""
    names = [name for name in guide_texts(plugin, review=review, worker_kind=worker_kind, execution_mode=execution_mode)
             if is_on_demand(name)]
    mode = guide_mode(review=review, worker_kind=worker_kind, execution_mode=execution_mode)
    return guidance_anchor({"mode": mode, "directory": GUIDE_DIRECTORY, "optional_files": dict.fromkeys(names)})


def guidance_anchor(guidance: dict) -> str:
    """Keep the active mode and its on-demand guides in standing instructions."""
    paths = ", ".join(f"{guidance['directory']}/{name}" for name in guidance.get("optional_files", {}))
    anchor = f"Current mode: {guidance['mode']}." + (f" On-demand guides: {paths}." if paths else "")
    if guidance["mode"] in MESSAGE_MODES:
        anchor += " This one-off correspondence turn preserves the underlying wait or idle intention."
    return anchor


def continuation_guidance(current: dict, previous: dict | None) -> dict | None:
    """The reading entry for a continuation: nothing while the mode and the on-demand
    guides are unchanged, since the required guides are standing instructions."""
    if (isinstance(previous, dict) and all(previous.get(key) == current.get(key)
                                           for key in ("mode", "directory", "optional_files"))):
        return None
    old = previous.get("optional_files", {}) if isinstance(previous, dict) else {}
    removed = sorted(set(old if isinstance(old, dict) else {}) - current.get("optional_files", {}).keys())
    result = dict(current)
    if removed:
        result["removed_guides"] = removed
        result["instruction"] = current["instruction"] + " Removed guides no longer apply."
    return result
