"""Small file-only public knowledge checks; no reading cursors or notifications."""
from __future__ import annotations

from .models import ValidationReport
from .wiki import KNOWLEDGE_README


def documentation_report(blobs, base_snapshot: str, snapshot: str) -> dict | None:
    """Use the cheap route only for an exact public, unpaired Markdown-only diff.

    Both manifests are frozen repository trees supplied by the runtime. A record
    kind is only a scheduling hint; it cannot authorize skipping domain checks.
    """
    details = {"validation_method": "public-documentation", "integration_snapshot": snapshot,
               "base_snapshot": base_snapshot, "fresh_integrated_validation": True,
               "peer_review_required": True, "proof_accepted": False}
    try:
        baseline = {item["path"]: item["sha256"] for item in blobs.get_json(base_snapshot)["files"]}
        final = {item["path"]: item["sha256"] for item in blobs.get_json(snapshot)["files"]}
        changed = {path for path in baseline.keys() | final.keys() if baseline.get(path) != final.get(path)}
        lean_stems = {path[:-5].upper().casefold() for path in baseline.keys() | final.keys()
                      if path.lower().endswith(".lean")}
        if any(not path.startswith(("wiki/", "notes/")) or not path.endswith(".md")
               or path[:-3].upper().casefold() in lean_stems for path in changed):
            return None
        if KNOWLEDGE_README not in final:
            raise ValueError("Public notes require a nonempty wiki/README.md entry point")
        readme = blobs.get(final[KNOWLEDGE_README]).decode("utf-8-sig")
        for path in sorted(changed | {KNOWLEDGE_README}):
            if path not in final:
                continue
            body = readme if path == KNOWLEDGE_README else blobs.get(final[path]).decode("utf-8-sig")
            if not body.strip():
                raise ValueError(f"Public note is empty: {path}")
        details.update(changed_paths=sorted(changed), checks=["public-markdown-only-diff", "readme-present", "utf8-nonempty"])
        report = ValidationReport(verdict="accepted", policy="general.peer@1", details=details)
    except OSError as error:
        report = ValidationReport(verdict="unavailable", policy="general.peer@1",
                                  details={**details, "reason": str(error)})
    except (ValueError, KeyError, TypeError) as error:
        report = ValidationReport(verdict="rejected", policy="general.peer@1",
                                  details={**details, "reason": str(error)})
    return report.model_dump(mode="json")
