"""Bounded, strict JSON object reading for native files and sync markers."""
from __future__ import annotations

import json


def read_json(body: bytes, *, max_bytes: int = 1048576) -> dict:
    if not body or len(body) > max_bytes:
        raise ValueError("A JSON document must contain at most one MiB of UTF-8 JSON")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object keys are not allowed")
            result[key] = value
        return result

    def invalid_number(_value):
        raise ValueError("Non-finite JSON values are not allowed")

    try:
        value = json.loads(body.decode("utf-8-sig"), object_pairs_hook=object_pairs,
                           parse_constant=invalid_number)
    except RecursionError as error:
        raise ValueError("JSON input is nested too deeply") from error
    if not isinstance(value, dict):
        raise ValueError("A JSON document must be a JSON object")
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 100 or visited > 100000:
            raise ValueError("JSON input structure exceeds its nesting or item limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value
