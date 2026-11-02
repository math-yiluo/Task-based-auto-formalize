"""Shared DAG checks use the same pure validator in native and standalone calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from formal_geyser.coordination import dag_check
from formal_geyser.coordination.dag_check import GraphError, parse_graph, validate_dags
from formal_geyser.coordination.snapshot_limits import MAX_SNAPSHOT_BYTES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "check_dags.py"
MODULE = "formal_geyser.coordination.dag_check"


def graph(*ids: str, edges: tuple[tuple[str, str], ...] = ()) -> dict:
    return {
        "nodes": [{"id": node_id, "title": f"Expected dependency {node_id}"} for node_id in ids],
        "edges": [{"from": source, "to": target} for source, target in edges],
    }


def test_blank_graphs_and_missing_mathematical_implementations_are_valid():
    validate_dags(graph(), graph())
    validate_dags(graph("definition"), graph("proof", edges=(("proof", "definition"),)))
    assert dag_check.MAX_GRAPH_BYTES == MAX_SNAPSHOT_BYTES


def test_cross_graph_edges_and_descriptive_references_are_preserved():
    definitions = graph("definition", "helper", edges=(("helper", "definition"),
                                                      ("definition", "prerequisite")))
    definitions["nodes"][0].update({
        "description": "An expected definition without an implementation.",
        "references": ["literature/paper.pdf#page=7", "https://example.invalid/source", "unknown-id"],
    })
    definitions["edges"][0]["reason"] = "Expected use in the helper definition."
    proofs = graph("proof", "prerequisite", edges=(("proof", "helper"),))
    original = deepcopy((definitions, proofs))
    parsed = parse_graph(json.dumps(definitions).encode("utf-8"), "proposed-definitions.json")
    assert parsed == definitions
    validate_dags(parsed, proofs)
    assert (definitions, proofs) == original


@pytest.mark.parametrize("definitions,proofs,location", [
    (graph("same", "same"), graph(), "definitions.json.nodes[1].id"),
    (graph("same"), graph("same"), "proofs.json.nodes[0].id"),
])
def test_duplicate_ids_are_rejected_globally(definitions, proofs, location):
    with pytest.raises(GraphError) as error:
        validate_dags(definitions, proofs)
    assert str(error.value) == (
        f"{location}: duplicate node ID 'same'; first declared at definitions.json.nodes[0].id"
    )


@pytest.mark.parametrize("document,detail", [
    ([], "expected a JSON object"),
    ({}, "missing required field(s): edges, nodes"),
    ({"nodes": [], "edges": [], "status": "accepted"}, "unknown field(s): 'status'"),
    ({"nodes": {}, "edges": []}, "nodes: expected an array"),
    ({"nodes": [], "edges": "none"}, "edges: expected an array"),
    ({"nodes": [None], "edges": []}, "nodes[0]: expected a JSON object"),
    ({"nodes": [{"id": "a"}], "edges": []}, "missing required field(s): title"),
    ({"nodes": [{"id": 1, "title": "A"}], "edges": []}, "id: expected a string"),
    ({"nodes": [{"id": " ", "title": "A"}], "edges": []}, "id: expected a nonempty string"),
    ({"nodes": [{"id": "a", "title": False}], "edges": []}, "title: expected a string"),
    ({"nodes": [{"id": "a", "title": ""}], "edges": []}, "title: expected a nonempty string"),
    ({"nodes": [{"id": "a", "title": "A", "kind": "proof"}], "edges": []},
     "unknown field(s): 'kind'"),
    ({"nodes": [{"id": "a", "title": "A", "current": True}], "edges": []},
     "unknown field(s): 'current'"),
    ({"nodes": [{"id": "a", "title": "A", "evidence": []}], "edges": []},
     "unknown field(s): 'evidence'"),
    ({"nodes": [{"id": "a", "title": "A", "description": []}], "edges": []},
     "description: expected a string"),
    ({"nodes": [{"id": "a", "title": "A", "references": "source"}], "edges": []},
     "references: expected an array of strings"),
    ({"nodes": [{"id": "a", "title": "A", "references": [None]}], "edges": []},
     "references[0]: expected a string"),
    ({"nodes": [], "edges": [[]]}, "edges[0]: expected a JSON object"),
    ({"nodes": [], "edges": [{"from": "a", "too": "b"}]}, "unknown field(s): 'too'"),
    ({"nodes": [], "edges": [{"from": "a"}]}, "missing required field(s): to"),
    ({"nodes": [], "edges": [{"from": None, "to": "a"}]}, "from: expected a string"),
    ({"nodes": [], "edges": [{"from": "a", "to": "b", "reason": 3}]},
     "reason: expected a string"),
    ({"nodes": [], "edges": [], 1: "invalid"}, "field names must be strings"),
])
def test_direct_call_rechecks_structure(document, detail):
    with pytest.raises(GraphError) as error:
        validate_dags(document, graph())
    assert detail in str(error.value)
    assert str(error.value).startswith("definitions.json")


@pytest.mark.parametrize("source,key", [
    ('{"nodes": [], "nodes": [], "edges": []}', "nodes"),
    ('{"nodes": [{"id": "a", "id": "b", "title": "A"}], "edges": []}', "id"),
    ('{"nodes": [], "edges": [{"from": "a", "to": "b", "to": "c"}]}', "to"),
])
def test_duplicate_json_keys_are_rejected_in_every_object(source, key):
    with pytest.raises(GraphError) as error:
        parse_graph(source, "edited.json")
    assert str(error.value) == f"edited.json: duplicate JSON key {key!r}"


@pytest.mark.parametrize("source,detail", [
    (b"\xff", "expected valid UTF-8 JSON"),
    ("\ud800", "expected valid UTF-8 JSON"),
    ('{"nodes":[{"id":"a","title":"\\ud800"}],"edges":[]}', "expected valid UTF-8 text"),
    ('{"nodes": [],\n "edges": [}', "invalid JSON at line 2, column 12"),
    ('{"nodes": [], "edges": [], "extra": NaN}', "non-finite JSON value 'NaN'"),
    ('{"nodes": [], "edges": Infinity}', "non-finite JSON value 'Infinity'"),
    ('{"nodes": [], "edges": -Infinity}', "non-finite JSON value '-Infinity'"),
    (b"", "invalid JSON at line 1, column 1"),
])
def test_bad_json_is_a_clear_graph_error(source, detail):
    with pytest.raises(GraphError) as error:
        parse_graph(source, "edited.json")
    assert detail in str(error.value)
    assert str(error.value).startswith("edited.json")


def test_deeply_nested_wrong_shape_is_reported_without_recursion_escaping():
    with pytest.raises(GraphError) as error:
        parse_graph("[" * 2000 + "]" * 2000, "nested.json")
    assert str(error.value) in {
        "nested.json: JSON input is nested too deeply",
        "nested.json: expected a JSON object",
    }


def test_utf8_bom_is_supported_without_altering_text():
    source = json.dumps(graph("unicode-\u03b1"), ensure_ascii=False)
    assert parse_graph("\ufeff" + source, "text") == graph("unicode-\u03b1")
    assert parse_graph(b"\xef\xbb\xbf" + source.encode(), "bytes") == graph("unicode-\u03b1")


@pytest.mark.parametrize("edge,detail", [
    (("missing", "definition"), ".from: unknown node ID 'missing'"),
    (("definition", "missing"), ".to: unknown node ID 'missing'"),
    (("proof", "definition"), ".from: node 'proof' belongs to proofs.json"),
    (("definition", "definition"), "self-loop 'definition' -> 'definition'"),
])
def test_edge_endpoints_ownership_and_self_loops(edge, detail):
    with pytest.raises(GraphError) as error:
        validate_dags(graph("definition", edges=(edge,)), graph("proof"))
    assert detail in str(error.value)


@pytest.mark.parametrize("label", ["definitions.json", "proofs.json"])
def test_duplicate_edges_are_rejected_even_when_the_reasons_differ(label):
    repeated = graph("a", "b", edges=(("a", "b"), ("a", "b")))
    repeated["edges"][0]["reason"] = "First description."
    repeated["edges"][1]["reason"] = "Another description of the same dependency."
    pair = (repeated, graph()) if label == "definitions.json" else (graph(), repeated)
    with pytest.raises(GraphError) as error:
        validate_dags(*pair)
    assert str(error.value) == f"{label}.edges[1]: duplicate edge 'a' -> 'b'"


def test_cycles_are_checked_over_both_graphs_with_the_exact_cycle_path():
    definitions = graph("a", "c", "prefix", edges=(("prefix", "a"), ("a", "b"), ("c", "a")))
    proofs = graph("b", edges=(("b", "c"),))
    with pytest.raises(GraphError) as error:
        validate_dags(definitions, proofs)
    assert str(error.value) == "Dependency cycle: 'a' -> 'b' -> 'c' -> 'a'"


def test_cycle_diagnostic_is_deterministic_after_node_and_edge_reordering():
    definitions = graph("a", "b", "c", "z", edges=(("a", "c"), ("a", "b"),
                                                     ("b", "a"), ("c", "a")))
    messages = []
    for _ in range(2):
        with pytest.raises(GraphError) as error:
            validate_dags(definitions, graph())
        messages.append(str(error.value))
        definitions["nodes"].reverse()
        definitions["edges"].reverse()
    assert messages == ["Dependency cycle: 'a' -> 'b' -> 'a'"] * 2


def test_long_chain_uses_iterative_traversal_without_a_smaller_node_count_cap():
    ids = [f"node-{index:05d}" for index in range(20_100)]
    source = json.dumps(graph(*ids, edges=tuple(zip(ids, ids[1:]))))
    parsed = parse_graph(source, "long-graph.json")
    validate_dags(parsed, graph())
    parsed["edges"].append({"from": ids[-1], "to": ids[-3]})
    with pytest.raises(GraphError) as error:
        validate_dags(parsed, graph())
    assert str(error.value) == (
        "Dependency cycle: 'node-20097' -> 'node-20098' -> 'node-20099' -> 'node-20097'"
    )


def test_document_limit_counts_utf8_bytes_and_accepts_the_exact_bound(monkeypatch):
    source = json.dumps(graph("\u03b1"), ensure_ascii=False)
    monkeypatch.setattr(dag_check, "MAX_GRAPH_BYTES", len(source.encode("utf-8")))
    assert parse_graph(source, "bounded.json") == graph("\u03b1")
    assert parse_graph(source.encode(), "bounded.json") == graph("\u03b1")
    for oversized in (source + " ", source.encode() + b" "):
        with pytest.raises(GraphError, match="exceeds the .*byte limit"):
            parse_graph(oversized, "bounded.json")


def test_small_graph_read_does_not_allocate_the_unused_byte_allowance(tmp_path):
    source = json.dumps(graph()).encode()
    path = tmp_path / "small.json"
    path.write_bytes(source)
    program = (
        "import json, sys, tracemalloc\n"
        "from pathlib import Path\n"
        "from formal_geyser.coordination.dag_check import _read_graph\n"
        "tracemalloc.start()\n"
        "body = _read_graph(Path(sys.argv[1]), 16 * 1024 * 1024)\n"
        "_, peak = tracemalloc.get_traced_memory()\n"
        "print(json.dumps({'file_bytes': len(body), 'peak': peak}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-S", "-c", program, str(path)], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    measured = json.loads(result.stdout)
    assert measured["file_bytes"] == len(source)
    assert measured["peak"] < 1024 * 1024


@pytest.mark.parametrize("change", ["grow", "shrink", "rewrite"])
def test_checker_rejects_files_changed_after_the_size_check(tmp_path, monkeypatch, change):
    path = tmp_path / "changing.json"
    source = json.dumps(graph("a")).encode()
    path.write_bytes(source)
    replacement = {
        "grow": source + b" ",
        "shrink": json.dumps(graph()).encode(),
        "rewrite": source.replace(b"Expected", b"Modified"),
    }[change]
    target = path
    original_open = Path.open

    @contextmanager
    def changing_open(selected, mode="r", *args, **kwargs):
        with original_open(selected, mode, *args, **kwargs) as stream:
            if selected != target or mode != "rb":
                yield stream
                return

            def changed_read(size):
                before = os.fstat(stream.fileno())
                with original_open(target, "wb") as writer:
                    writer.write(replacement)
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                return stream.read(size)

            yield SimpleNamespace(read=changed_read, fileno=stream.fileno)

    monkeypatch.setattr(Path, "open", changing_open)
    with pytest.raises(GraphError, match="graph file changed during the read"):
        dag_check._read_graph(path, 1024 * 1024)


def test_cli_checks_the_combined_byte_limit(tmp_path, monkeypatch, capsys):
    source = json.dumps(graph()).encode()
    definitions = tmp_path / "definitions.json"
    proofs = tmp_path / "proofs.json"
    definitions.write_bytes(source)
    proofs.write_bytes(source)
    monkeypatch.setattr(dag_check, "MAX_GRAPH_BYTES", len(source) * 2)
    assert dag_check.main([str(definitions), str(proofs)]) == 0
    capsys.readouterr()
    proofs.write_bytes(source + b" ")
    assert dag_check.main([str(definitions), str(proofs)]) == 1
    output = capsys.readouterr()
    assert "combined graph documents exceed" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("entry", ["script", "module", "copied"])
def test_cli_needs_only_stdlib_and_checks_files_without_installation(tmp_path, entry):
    definitions = tmp_path / "definitions.json"
    proofs = tmp_path / "proofs.json"
    definitions.write_text(json.dumps(graph("definition")), encoding="utf-8")
    proofs.write_text(json.dumps(graph("proof", edges=(("proof", "definition"),))),
                      encoding="utf-8")
    command = [sys.executable, "-S"]
    if entry == "copied":
        copied = tmp_path / "check_dags.py"
        copied.write_bytes(Path(dag_check.__file__).read_bytes())
        command.append(str(copied))
    else:
        command += [str(SCRIPT)] if entry == "script" else ["-m", MODULE]
    environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
    if entry != "module":
        environment.pop("PYTHONPATH", None)

    def run(*arguments):
        return subprocess.run(command + list(arguments), cwd=tmp_path, env=environment,
                              capture_output=True, text=True, check=False)

    help_result = run("--help")
    assert help_result.returncode == 0
    assert "definitions" in help_result.stdout and "proofs" in help_result.stdout
    assert "No Lean files" in " ".join(help_result.stdout.split())
    valid = run(str(definitions), str(proofs))
    assert valid.returncode == 0, valid.stderr
    assert "DAG validation passed" in valid.stdout
    proofs.write_text(json.dumps(graph("proof", edges=(("proof", "missing"),))), encoding="utf-8")
    invalid = run(str(definitions), str(proofs))
    assert invalid.returncode == 1
    assert "unknown node ID 'missing'" in invalid.stderr
    assert "Traceback" not in invalid.stderr
    malformed = run(str(definitions))
    assert malformed.returncode == 2
    assert "Traceback" not in malformed.stderr
    missing = run(str(definitions), str(tmp_path / "missing.json"))
    assert missing.returncode == 1
    assert "cannot read graph file" in missing.stderr
    assert "Traceback" not in missing.stderr
