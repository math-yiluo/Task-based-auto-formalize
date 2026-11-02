"""Optional reviewer notes that help the Integrator organize an accepted contribution.

A reviewer checks whether the candidate duplicates or overlaps existing Library content
and where its files belong, and may record what it found with the acceptance. The notes
are kept in the acceptance report and shown in the Integrator's batch view. They are
not acceptance evidence: they never carry qualifications or approve changed mathematics.
"""
from __future__ import annotations

from typing import Any

from .errors import HubError

RELATIONS = ("duplicate", "generalizes", "specializes", "related")
MAX_OVERLAPS = 20
LIMITS = {"existing": 500, "note": 2000, "placement": 2000, "remarks": 4000}

SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "minProperties": 1,
    "description": "Optional, with an acceptance only: what your duplication and placement checks found, "
                   "for the Integrator's organization of the Library. Omit it when there is nothing to add.",
    "properties": {
        "overlaps": {
            "type": "array", "minItems": 1, "maxItems": MAX_OVERLAPS,
            "items": {
                "type": "object", "additionalProperties": False, "required": ["existing", "relation", "note"],
                "properties": {
                    "existing": {"type": "string", "minLength": 1, "maxLength": LIMITS["existing"],
                                 "description": "An existing Library declaration, module or page."},
                    "relation": {"type": "string", "enum": list(RELATIONS),
                                 "description": "duplicate: the same notion; generalizes or specializes: what this "
                                                "contribution is to it; related: overlapping but different."},
                    "note": {"type": "string", "minLength": 1, "maxLength": LIMITS["note"],
                             "description": "How they relate, and whether one could replace the other "
                                            "without changing the meaning of any definition or statement."},
                },
            },
        },
        "placement": {"type": "string", "minLength": 1, "maxLength": LIMITS["placement"],
                      "description": "Where these files belong in the Library (folder or module) and why, "
                                     "when that differs from the submitted paths."},
        "remarks": {"type": "string", "minLength": 1, "maxLength": LIMITS["remarks"],
                    "description": "Anything else the Integrator needs to organize this contribution."},
    },
}


def checked_integration_notes(value: Any) -> dict:
    """Return the notes unchanged after bounds checks; their content is the reviewer's."""
    def text(item, field):
        return isinstance(item, str) and bool(item.strip()) and len(item) <= LIMITS[field]

    if not isinstance(value, dict) or not value or set(value) - {"overlaps", "placement", "remarks"}:
        raise HubError("INVALID_INPUT", "integration_notes takes one or more of overlaps, placement and remarks", 422)
    for field in ("placement", "remarks"):
        if field in value and not text(value[field], field):
            raise HubError("INVALID_INPUT", f"integration_notes.{field} must be nonblank text of at most "
                                            f"{LIMITS[field]} characters", 422)
    if "overlaps" in value:
        overlaps = value["overlaps"]
        if not isinstance(overlaps, list) or not 1 <= len(overlaps) <= MAX_OVERLAPS or not all(
                isinstance(item, dict) and set(item) == {"existing", "relation", "note"}
                and text(item["existing"], "existing") and item["relation"] in RELATIONS
                and text(item["note"], "note") for item in overlaps):
            raise HubError("INVALID_INPUT", f"integration_notes.overlaps lists 1 to {MAX_OVERLAPS} existing items, "
                                            "each with existing, relation (" + ", ".join(RELATIONS) + ") and note", 422)
    return value
