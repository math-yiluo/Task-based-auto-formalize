"""One machine validation of the complete final tree, recorded for every contribution."""
from __future__ import annotations

from .coordination.knowledge import documentation_report
from .coordination.storage import collected_thread_call


async def validate_batch(plugin, hub, items, snapshot_sha, *, base_snapshot=None):
    """Each report carries its contribution's publication policy and the exact final snapshot."""
    if base_snapshot is not None and items and all(item["artifact"]["kind"] == "note.wiki" for item in items):
        report = await collected_thread_call(documentation_report, hub.blobs, base_snapshot, snapshot_sha)
        if report is not None:
            return {item["artifact"]["id"]: {**report, "policy": item["task"]["contract"]["acceptance_policy"]}
                    for item in items}
    final_tree = getattr(plugin, "validate_final_tree", None)
    if callable(final_tree):
        report = (await final_tree(hub, items, snapshot_sha)).model_dump(mode="json")
        return {item["artifact"]["id"]: {**report, "policy": item["task"]["contract"]["acceptance_policy"]}
                for item in items}
    reports = {}
    for item in items:
        report = await plugin.validate_candidate(hub, item["task"], {**item["artifact"], "snapshot": snapshot_sha})
        report.details.update(integration_snapshot=snapshot_sha, fresh_integrated_validation=True)
        reports[item["artifact"]["id"]] = report.model_dump(mode="json")
    return reports
