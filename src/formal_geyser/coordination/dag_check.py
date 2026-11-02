"""Validate the two advisory shared DAGs using only the Python standard library.

Each document contains ``nodes`` and ``edges`` arrays. Nodes have required ``id``
and ``title`` strings, optional ``description`` text, and optional ``references``
containing descriptive source links. Edges have required ``from`` and ``to`` node
IDs and optional ``reason`` text. An edge means that its source depends on its
target; its source must belong to the document containing the edge. Targets may
belong to either document. References are not edges and are never dereferenced.

These graphs describe expected dependencies; they do not certify mathematical
implementation, acceptance, or current evidence. Empty graphs are valid. Node
IDs are globally unique, and the union of both documents must be acyclic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

# Match coordination.snapshot_limits.MAX_SNAPSHOT_BYTES without importing the
# package: the runtime also copies this module into worker workspaces as a script.
MAX_GRAPH_BYTES = 1024 * 1024 * 1024


class GraphError(ValueError):
    """A shared graph has invalid JSON, structure, or dependency relations."""


def _object(
    value: object, label: str, *, required: set[str], optional: set[str] | None = None,
) -> dict:
    if not isinstance(value, dict):
        raise GraphError(f"{label}: expected a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise GraphError(f"{label}: field names must be strings")
    unknown = sorted(set(value) - required - (optional or set()))
    if unknown:
        raise GraphError(f"{label}: unknown field(s): {', '.join(repr(key) for key in unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise GraphError(f"{label}: missing required field(s): {', '.join(missing)}")
    return value


def _string(value: object, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, str):
        raise GraphError(f"{label}: expected a string")
    if nonempty and not value.strip():
        raise GraphError(f"{label}: expected a nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise GraphError(f"{label}: expected valid UTF-8 text") from None


def _structure(value: object, label: str) -> dict:
    document = _object(value, label, required={"nodes", "edges"})
    for name in ("nodes", "edges"):
        if not isinstance(document[name], list):
            raise GraphError(f"{label}.{name}: expected an array")
    for index, item in enumerate(document["nodes"]):
        location = f"{label}.nodes[{index}]"
        node = _object(item, location, required={"id", "title"},
                       optional={"description", "references"})
        for name in ("id", "title"):
            _string(node[name], f"{location}.{name}", nonempty=True)
        if "description" in node:
            _string(node["description"], f"{location}.description")
        if "references" in node:
            if not isinstance(node["references"], list):
                raise GraphError(f"{location}.references: expected an array of strings")
            for reference_index, reference in enumerate(node["references"]):
                _string(reference, f"{location}.references[{reference_index}]", nonempty=True)
    for index, item in enumerate(document["edges"]):
        location = f"{label}.edges[{index}]"
        edge = _object(item, location, required={"from", "to"}, optional={"reason"})
        for name in ("from", "to"):
            _string(edge[name], f"{location}.{name}", nonempty=True)
        if "reason" in edge:
            _string(edge["reason"], f"{location}.reason")
    return document


def parse_graph(data: bytes | str, label: str) -> dict:
    """Parse one bounded UTF-8 JSON graph without silently dropping duplicate keys.

    This checks the document's shape. Call :func:`validate_dags` with both parsed
    documents to check global IDs, edge ownership, endpoints, and cycles. Native
    callers should enforce the same byte envelope over their combined captured
    inputs before atomically saving both documents.
    """
    if not isinstance(data, (bytes, str)):
        raise GraphError(f"{label}: expected bytes or a string containing JSON")
    try:
        size = len(data) if isinstance(data, bytes) else len(data.encode("utf-8"))
    except UnicodeEncodeError:
        raise GraphError(f"{label}: expected valid UTF-8 JSON") from None
    if size > MAX_GRAPH_BYTES:
        raise GraphError(f"{label}: graph document exceeds the {MAX_GRAPH_BYTES}-byte limit")
    try:
        source = data.decode("utf-8-sig") if isinstance(data, bytes) else data.removeprefix("\ufeff")
    except UnicodeDecodeError:
        raise GraphError(f"{label}: expected valid UTF-8 JSON") from None

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise GraphError(f"{label}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise GraphError(f"{label}: non-finite JSON value {value!r} is not allowed")

    try:
        document = json.loads(source, object_pairs_hook=object_pairs,
                              parse_constant=invalid_constant)
    except GraphError:
        raise
    except json.JSONDecodeError as error:
        raise GraphError(
            f"{label}: invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from None
    except RecursionError:
        raise GraphError(f"{label}: JSON input is nested too deeply") from None
    except ValueError as error:
        raise GraphError(f"{label}: invalid JSON: {error}") from None
    return _structure(document, label)


def _check_cycles(adjacency: dict[str, set[str]]) -> None:
    """Iterative DFS, ordered by node ID so the reported cycle is reproducible."""
    finished: set[str] = set()
    active: dict[str, int] = {}
    path: list[str] = []
    for start in sorted(adjacency):
        if start in finished:
            continue
        active[start] = 0
        path.append(start)
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(adjacency[start])))]
        while stack:
            node, neighbors = stack[-1]
            target = next(neighbors, None)
            if target is None:
                stack.pop()
                path.pop()
                del active[node]
                finished.add(node)
            elif target in active:
                cycle = path[active[target]:] + [target]
                raise GraphError("Dependency cycle: " + " -> ".join(repr(item) for item in cycle))
            elif target not in finished:
                active[target] = len(path)
                path.append(target)
                stack.append((target, iter(sorted(adjacency[target]))))


def validate_dags(definitions: dict, proofs: dict) -> None:
    """Validate both advisory graphs together without modifying either document.

    ``references`` strings are descriptive links; only ``edges`` participate in
    dependency validation. This function also checks shape for callers supplying
    dictionaries directly. Use :func:`parse_graph` on captured file bytes first
    to reject malformed JSON and duplicate JSON keys before calling this function.
    """
    documents = (
        ("definitions.json", _structure(definitions, "definitions.json")),
        ("proofs.json", _structure(proofs, "proofs.json")),
    )
    owners: dict[str, str] = {}
    locations: dict[str, str] = {}
    adjacency: dict[str, set[str]] = {}
    for label, document in documents:
        for index, node in enumerate(document["nodes"]):
            node_id = node["id"]
            location = f"{label}.nodes[{index}].id"
            if node_id in owners:
                raise GraphError(
                    f"{location}: duplicate node ID {node_id!r}; first declared at {locations[node_id]}"
                )
            owners[node_id] = label
            locations[node_id] = location
            adjacency[node_id] = set()
    for label, document in documents:
        for index, edge in enumerate(document["edges"]):
            source, target = edge["from"], edge["to"]
            location = f"{label}.edges[{index}]"
            if source not in owners:
                raise GraphError(f"{location}.from: unknown node ID {source!r}")
            if owners[source] != label:
                raise GraphError(
                    f"{location}.from: node {source!r} belongs to {owners[source]}; "
                    f"the source must belong to {label}"
                )
            if target not in owners:
                raise GraphError(f"{location}.to: unknown node ID {target!r}")
            if source == target:
                raise GraphError(f"{location}: self-loop {source!r} -> {target!r} is not allowed")
            if target in adjacency[source]:
                raise GraphError(f"{location}: duplicate edge {source!r} -> {target!r}")
            adjacency[source].add(target)
    _check_cycles(adjacency)


def _read_graph(path: Path, remaining_bytes: int) -> bytes:
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > remaining_bytes:
                raise GraphError(
                    f"{path}: combined graph documents exceed the {MAX_GRAPH_BYTES}-byte limit"
                )
            body = stream.read(min(before.st_size + 1, remaining_bytes + 1))
            after = os.fstat(stream.fileno())
    except GraphError:
        raise
    except (OSError, ValueError) as error:
        raise GraphError(f"{path}: cannot read graph file: {error}") from None
    if len(body) > remaining_bytes:
        raise GraphError(
            f"{path}: combined graph documents exceed the {MAX_GRAPH_BYTES}-byte limit"
        )
    if (len(body) != before.st_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise GraphError(f"{path}: graph file changed during the read; rerun the checker")
    return body


def main(argv: Sequence[str] | None = None) -> int:
    """Check two explicitly selected graph files; return zero only when valid."""
    parser = argparse.ArgumentParser(
        description="Check the shared definitions and proofs DAGs together.",
        epilog=(
            "Edges point from a dependent to a prerequisite. IDs are unique across both "
            "files; cross-file targets are allowed. References are descriptive links. "
            "No Lean files or mathematical implementation are required or checked. "
            "Exits 0 for valid graphs, 1 for invalid/unreadable graphs, or 2 for usage errors."
        ),
    )
    parser.add_argument("definitions", type=Path, help="path to the proposed definitions JSON file")
    parser.add_argument("proofs", type=Path, help="path to the proposed proofs JSON file")
    args = parser.parse_args(argv)
    try:
        definitions_data = _read_graph(args.definitions, MAX_GRAPH_BYTES)
        proofs_data = _read_graph(args.proofs, MAX_GRAPH_BYTES - len(definitions_data))
        definitions = parse_graph(definitions_data, str(args.definitions))
        proofs = parse_graph(proofs_data, str(args.proofs))
        validate_dags(definitions, proofs)
    except GraphError as error:
        print(f"DAG validation failed: {error}", file=sys.stderr)
        return 1
    print("DAG validation passed: definitions and proofs form an acyclic graph.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
