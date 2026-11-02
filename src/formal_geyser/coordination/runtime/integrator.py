"""Integrator operations behind the unchanged TaskHub gateways."""

from __future__ import annotations

from ..models import Actor, Role
from ..storage import collected_thread_call
from .tools import _function


def integrator_operations():
    text = {"type": "string", "minLength": 1}
    return [
        _function(
            "taskhub_batch_status",
            "Inspect your fixed batch, exact round, per-artifact evidence, and deferred contributions.",
            {},
            [],
        ),
        _function(
            "taskhub_batch_prepare",
            "Prepare one combined workspace from the included accepted contributions. A fresh preparation after deferral excludes its files and retains earlier workspaces.",
            {},
            [],
        ),
        _function(
            "taskhub_batch_resolve",
            "Explicitly stage resolved or newly added paths in the current integration workspace. Resolve conflicts before validation; do not introduce unreviewed mathematics.",
            {"paths": {"type": "array", "items": text, "minItems": 1}},
            ["paths"],
        ),
        _function(
            "taskhub_batch_validate",
            "Freeze and validate the combined tree. Public Markdown-only changes use document checks without Lean; other changes use normal domain validation. Returns exact per-contribution reports.",
            {},
            [],
        ),
        _function(
            "taskhub_batch_publish",
            "Submit the exact validated batch round for publication after process quiescence. The original independent reviews remain the semantic acceptance; you remain responsible for a semantically consistent integrated Library.",
            {"round_id": text},
            ["round_id"],
        ),
        _function(
            "taskhub_batch_defer",
            "Isolate a problematic contribution with a reason; rebuild and revalidate the remaining batch before publication. This is not mathematical rejection or withdrawal.",
            {"candidate_id": text, "reason": text},
            ["candidate_id", "reason"],
        ),
        _function(
            "taskhub_batch_retry",
            "Return an accepted deferred contribution to the next-batch queue when new evidence or an answer resolves its blocker. Explain the change; do not retry unchanged failures.",
            {"candidate_id": text, "reason": text},
            ["candidate_id", "reason"],
        ),
        _function(
            "taskhub_batch_finish_empty",
            "Finish a batch in which no contribution remains included: defer each one first, a withdrawn one too.",
            {},
            [],
        ),
    ]


def _contribution_summary(artifact: dict, report: dict) -> str:
    """One line per contribution: its kind, its review verdict and the start of the rationale."""
    rationale = " ".join(str(report.get("rationale") or "").split())
    line = f"{artifact['kind']} {artifact['id']}: review {report.get('verdict', 'approve')}"
    return line + (f"; {rationale[:200]}" + ("…" if len(rationale) > 200 else "") if rationale else "")


async def dispatch_integrator(runner, attempt, name, args, key, native_call=None):
    actor = runner._worker_actor(attempt)
    bound = Actor(
        role=Role.RUNNER,
        worker_id=actor.worker_id,
        attempt_id=actor.attempt_id,
        fencing_token=actor.fencing_token,
    )

    async def command(name, payload, *, actor=actor, suffix=""):
        return await runner._command(
            name,
            payload,
            actor=actor,
            key=key + suffix,
            native_call=native_call
            if name in {"integrator.publish", "integrator.finish_empty"}
            else None,
        )

    if name == "taskhub_batch_prepare":
        return await runner.hub.prepare_integration_batch(actor, command)
    if name == "taskhub_batch_resolve":
        return await runner.hub.resolve_integration_batch(actor, args["paths"])
    if name == "taskhub_batch_validate":
        from formal_geyser.batch_validation import validate_batch

        batch = await command("integrator.status", {}, suffix=":basis")
        frozen = await runner.hub.freeze_integration_batch(actor)
        batch = await command(
            "integrator.freeze.store",
            {"batch_id": batch["id"], "workspace_id": batch["workspace_id"], "frozen": frozen},
            actor=bound,
            suffix=":freeze",
        )
        items = await runner.hub.batch_items_for_validation(actor, batch["round_id"])
        base = None
        if items and all(item["artifact"]["kind"] == "note.wiki" for item in items):
            base = await collected_thread_call(runner.hub._repository().snapshot,
                                               batch["round"]["target_head"], runner.hub.blobs)
        reports = await validate_batch(runner.plugin, runner.hub, items, frozen["snapshot"],
                                       base_snapshot=base)
        result = await command(
            "integrator.validation",
            {"batch_id": batch["id"], "round_id": batch["round_id"], "reports": reports},
            actor=bound,
            suffix=":validation",
        )
        return result
    operation = {
        "taskhub_batch_status": "integrator.status",
        "taskhub_batch_publish": "integrator.publish",
        "taskhub_batch_defer": "integrator.defer",
        "taskhub_batch_retry": "integrator.retry",
        "taskhub_batch_finish_empty": "integrator.finish_empty",
    }[name]
    result = await command(operation, args)
    if name == "taskhub_batch_status":
        from ..material_files import DeferredMaterial
        from .review_entry import _author_introduction, _candidate_entry, _detail, host_entry

        # References now; the files after the loop, off the event loop.
        material = DeferredMaterial(runner.hub.workspace(attempt["worker_id"]))

        result["contributions"] = []
        for entry in await runner.hub.batch_contributions(actor):
            item, artifact = entry["item"], entry["artifact"]
            report = entry["acceptance"]["report"]
            contribution = {
                "candidate_id": item["candidate_id"], "state": item["state"],
                # Enough to plan the batch; open the files below only where a summary leaves a question.
                "summary": _contribution_summary(artifact, report),
                "candidate": _candidate_entry(artifact, material),
                "introduction": _author_introduction(material, entry["review"], artifact),
                "independent_review": material.json(report),
                "acceptance_conditions": host_entry(entry["host"], entry["conditions"], material),
            }
            if report.get("integration_notes"):
                # The reviewer's duplication and placement findings, shown beside the review.
                contribution["integration_notes"] = _detail(material, report["integration_notes"])
            result["contributions"].append(contribution)
        await collected_thread_call(material.flush)
    return result
