"""Read the exact complete message represented by a native delivery reference."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def native_text(message):
    reference = message.get("native_message") or message["file"]
    body = Path(reference["path"]).read_bytes()
    assert Path(reference["path"]).is_absolute()
    if "sha256" in reference:
        assert hashlib.sha256(body).hexdigest() == reference["sha256"]
    if reference.get("format") == "json" or body.lstrip().startswith(b"{"):
        return json.loads(body)["text"]
    return body.decode("utf-8").partition("\n```\n\n")[2].removesuffix("\n")
