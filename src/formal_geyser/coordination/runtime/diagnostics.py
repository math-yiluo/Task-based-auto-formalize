"""Bounded, credential-redacted native error diagnostics."""
from __future__ import annotations

import json
import re
from typing import Any

_CREDENTIAL_LINE = re.compile(
    r"""(?ix)(?<![A-Za-z0-9])(?:authorization|proxy-authorization|set-cookie|cookie|api[-_ ]?key|
    access[-_ ]?token|refresh[-_ ]?token|id[-_ ]?token|session[-_ ]?token|
    client[-_ ]?secret|password|secret|token)
    \b[\s\\"']*[:=]"""
)


def safe_message(value: str, *, max_bytes: int = 4096) -> str:
    lines = []
    for line in value[:65536].splitlines():
        if _CREDENTIAL_LINE.search(line):
            lines.append("[Credential-bearing diagnostic line redacted]")
            continue
        line = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", line)
        line = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", line)
        line = re.sub(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
                      "[REDACTED]", line)
        line = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                      "[REDACTED_EMAIL]", line)
        lines.append(line)
    text = "\n".join(lines).strip() or "Native error supplied no message"
    body = text.encode("utf-8")
    if len(body) > max_bytes:
        text = body[:max_bytes].decode("utf-8", errors="ignore") + " [truncated]"
    return text


def normalize_error(error: Any, *, source: str, will_retry: bool | None = None) -> dict[str, Any]:
    if isinstance(error, BaseException):
        native = getattr(error, "error", None)
        error = native if isinstance(native, dict) else {
            "message": str(error) or type(error).__name__, "code": type(error).__name__,
        }
    if isinstance(error, str):
        try:
            decoded = json.loads(error)
        except (ValueError, TypeError):
            decoded = None
        error = decoded if isinstance(decoded, dict) else {"message": error}
    if not isinstance(error, dict):
        error = {"message": "Native error supplied no structured diagnostic"}
    nested = error.get("error")
    if isinstance(nested, dict):
        error = {**error, **nested}
    message = error.get("message")
    result: dict[str, Any] = {
        "source": source,
        "message": safe_message(message if isinstance(message, str) else
                                "Native error supplied no message"),
    }
    code = error.get("code", error.get("type", error.get("codexErrorInfo")))
    if isinstance(code, dict):
        variants = [key for key in code if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", key)]
        code = variants[0] if variants else None
    if type(code) is int or (isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code)):
        result["code"] = code
    status = error.get("status", error.get("httpStatusCode"))
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    details = error.get("additionalDetails")
    if isinstance(details, str) and details:
        result["additional_details"] = safe_message(details, max_bytes=2048)
    if type(will_retry) is bool:
        result["will_retry"] = will_retry
    return result

