"""Author native fixture files before exercising the real worker gateway."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def file_arguments(workspace: Path, operation: str, arguments: dict) -> dict:
    """Fixture authors may construct Python data; workers submit native paths."""
    payload = None
    field = None
    remaining = {}
    if operation in {"stage", "contribute"} and "manifest_path" not in arguments:
        payload, field = arguments, "manifest_path"
    elif operation in {"publish", "propose_revision"} and "contract" in arguments:
        payload, field = arguments["contract"], "contract_path"
        remaining = {key: value for key, value in arguments.items() if key != "contract"}
    if field is None:
        return arguments
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    relative = ".private/tool-fixtures/" + hashlib.sha256(body.encode()).hexdigest() + ".json"
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return {**remaining, field: relative}
