from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from formal_geyser.coordination.content import PRIVATE_DIRECTORY
from formal_geyser.coordination.models import Actor, Command, Role, StrictModel

from .lean_service import client as lean_service
from .paths import safe_child
from .registry import ObjectRegistry, RegistryError, source_directory
from .schemas import OBJECT_TYPES, ArtifactId, LeanName, RelativePath


class CheckArguments(StrictModel):
    directory: RelativePath | None = None
    environment_artifact: ArtifactId
    modules: Annotated[list[LeanName], Field(min_length=1, max_length=100)]
    declaration: LeanName | None = None


class ListArguments(StrictModel):
    object_type: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=100)


TOOL_ARGUMENTS: dict[str, type[StrictModel]] = {
    "lean_check": CheckArguments,
    "lean_registry_list": ListArguments,
}
DESCRIPTIONS = {
    "lean_check": "Compile your Lean files, or a draft in .private/drafts/<name> with them, optionally inspecting a declaration. It reads your workspace's Lean files where they are; a draft's files take the place of workspace files of the same module. It needs an approved environment that permits worker local execution. The project's Lean service runs it; its build outputs stay out of your workspace. This is a development check, never proof acceptance.",
    "lean_registry_list": "Read a page of shared domain records and receipt summaries. Private notes remain in native files. This query does not schedule work.",
}
WORKER_INSTRUCTIONS = """Lean work has no required reasoning order. Every Lean file in your workspace outside .private/
has a nonempty Markdown file with the same path and name; statements are Prop-valued def/abbrev; never use
sorry, admit, or new axioms. Read submission procedures only when preparing a submission.
"""


def worker_tools() -> list[dict]:
    return [{"type": "function", "name": name, "description": DESCRIPTIONS[name],
             "inputSchema": arguments.model_json_schema()}
            for name, arguments in TOOL_ARGUMENTS.items()]


def artifact_summary(artifact: dict) -> dict:
    data = artifact["data"]
    summary = {"id": artifact["id"], "kind": artifact["kind"], "status": artifact.get("status"),
            "category": artifact.get("category", "note"), "content_file": artifact.get("content_file"),
            "accepted": bool(artifact.get("accepted")), "public": bool(artifact.get("public")),
            "policy": artifact.get("publication_policy"), "review_task_id": artifact.get("review_task_id"),
            "name": data.get("name"),
            "version": data.get("version"), "input_refs": artifact.get("input_refs", []),
            "evidence": [{"id": x.get("id"), "policy": x["policy"], "verdict": x["verdict"],
                          "qualifications": x.get("qualifications", [])}
                         for x in artifact.get("evidence", [])[-20:]]}
    if artifact.get("review_task_id"):
        summary["artifact_id"] = artifact["id"]
        if artifact.get("category") == "note":
            summary["note_id"] = artifact["id"]
        summary["publication_policy"] = artifact.get("publication_policy")
    return summary


def _workspace_directory(hub, actor: Actor, relative: str | None) -> Path:
    if actor.role != Role.WORKER or not actor.worker_id or not actor.attempt_id or actor.fencing_token is None:
        raise RegistryError("Domain tools require a fenced active worker")
    workspace = hub.workspace(actor.worker_id)
    directory = safe_child(workspace, relative) if relative else workspace
    if not directory.is_dir():
        raise RegistryError("The workspace source directory does not exist")
    return directory


def _draft(hub, actor: Actor, directory: Path) -> Path | None:
    """A draft directory under .private/drafts, which compiles over the rest of the workspace;
    nothing else private is Lean source."""
    private = (hub.workspace(actor.worker_id) / PRIVATE_DIRECTORY).resolve()
    resolved = directory.resolve()
    if not resolved.is_relative_to(private):
        return None
    if resolved == private / "drafts" or not resolved.is_relative_to(private / "drafts"):
        raise RegistryError("Of your private files, only a draft directory .private/drafts/<name> is Lean source")
    return resolved


async def handle_worker_tool(hub, name: str, arguments: dict, actor: Actor, *, key: str) -> dict:
    """The runtime validates execution ownership before dispatch; mutations also fence atomically."""
    if name not in TOOL_ARGUMENTS:
        raise RegistryError("Unknown Lean worker tool")
    await hub.authorize_worker(actor)
    parsed: Any = TOOL_ARGUMENTS[name].model_validate(arguments)
    registry = ObjectRegistry(hub)
    if name == "lean_registry_list":
        if parsed.object_type is not None and parsed.object_type not in OBJECT_TYPES:
            raise RegistryError("Unknown registry object type")
        page = await hub.list_visible_records(actor,
            kind=f"lean.{parsed.object_type}" if parsed.object_type else None, kind_prefix="lean.",
            offset=parsed.offset, limit=parsed.limit, exclude_private=True)
        return {**page, "items": [artifact_summary(item) for item in page["items"]]}
    prepared = await hub.execution_environment()
    if prepared is not None and parsed.environment_artifact != prepared["environment_artifact"]:
        raise RegistryError("Use this project's prepared Lean environment for development checks")
    ensure_copy = getattr(hub, "ensure_library_copy", None)
    if ensure_copy is not None:
        await ensure_copy(actor.worker_id)  # A reviewer's workspace has no Library until it compiles.
    directory = _workspace_directory(hub, actor, parsed.directory)
    overlay = _draft(hub, actor, directory)
    if overlay is not None:
        directory = hub.workspace(actor.worker_id)
    record, _ = await registry.get(parsed.environment_artifact, "environment", approved=True)
    if record.local_execution != "trusted-code-only":
        return {"verdict": "unavailable", "proof_accepted": False,
                "reason": "This approved environment does not permit local Lean execution"}
    directory = await asyncio.to_thread(source_directory, hub, directory)
    # A development check, never acceptance: the project's Lean service compiles the working
    # files where they are, keeps its outputs out of the workspace and skips the kernel replay.
    checked = await lean_service.check(
        Path(hub.root), record, hub.blobs, directory=directory, overlay=overlay, modules=parsed.modules,
        kernel_replay=False,
        inspections=[{"module": parsed.modules[-1], "declaration": parsed.declaration}] if parsed.declaration else [],
        lane="interactive", owner=actor.worker_id or "worker", label="lean_check " + ", ".join(parsed.modules[:3]))
    if not checked["doctor"]["ok"]:
        return {"verdict": "unavailable", "proof_accepted": False, "doctor": checked["doctor"]}
    result = {**checked["compilation"], "sources_sha256": checked["sources_sha256"]}
    if checked["inspections"]:
        result["inspection"] = checked["inspections"][0]
        result["ok"] = result["inspection"]["ok"]
    report = await hub.execute(Command(name="record.private", key=key,
                                       payload={"kind": "tool.lean-development", "data": result,
                                                "input_refs": [parsed.environment_artifact]}), actor)
    summary = {"ok": result["ok"], "proof_accepted": False, "sources_sha256": checked["sources_sha256"],
               "diagnostic_id": report["id"], "content_file": report.get("content_file"), "modules": []}
    summary["build"] = {k: result[k] for k in ("validation_method", "built_modules", "reused_modules", "cache_retention",
                                               "session") if k in result}
    if result.get("reason"):
        summary["reason"] = result["reason"]
    # A Lake process reports the complete build once, not once per target.
    diagnostic = result.get("diagnostics", {})
    summary["diagnostics"] = {"errors": diagnostic.get("errors", 0),
                              "warnings": diagnostic.get("warnings", 0),
                              "messages": diagnostic.get("messages", [])[:20]}
    summary["returncode"] = result.get("process", {}).get("returncode")
    summary["modules"] = [{"module": item["module"]} for item in result["modules"]]
    if inspection := result.get("inspection"):
        summary["inspection"] = {key: value for key, value in inspection.get("metadata", {}).items()
                                 if key != "type_repr"}
    return summary
