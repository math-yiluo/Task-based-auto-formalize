"""Disjoint stale DAG changes merge without bypassing graph or version checks."""
import json
from pathlib import Path

import pytest

from formal_geyser.coordination.dag_check import GraphError
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.shared_dags import DagMergeConflict, merge_dag_files

from .test_gateway_runtime import call
from .test_gateway_runtime import project as project


def pair(*nodes, edges=()):
    return {"definitions.json": json.dumps({"nodes": [{"id": n, "title": n} for n in nodes],
            "edges": list(edges)}).encode(), "proofs.json": b'{"nodes": [], "edges": []}'}


def test_three_way_merge_rejects_overlap_and_combined_dangling_reference():
    before = pair("A", "B")
    ours, theirs = pair("A", "B"), pair("A", "B")
    for tree, title in ((ours, "left"), (theirs, "right")):
        doc = json.loads(tree["definitions.json"])
        doc["nodes"][0]["title"] = title
        tree["definitions.json"] = json.dumps(doc).encode()
    with pytest.raises(DagMergeConflict):
        merge_dag_files(before, ours, theirs)
    with pytest.raises(GraphError):
        merge_dag_files(before, pair("A"), pair("A", "B", edges=[{"from": "A", "to": "B"}]))


async def test_stale_disjoint_dag_save_merges_and_overlap_keeps_committed_head(project):
    hub, _, attempt, runner = project
    async def save(files, expected, key):
        folder = hub.workspace(attempt["worker_id"]) / ".private/dags" / key
        folder.mkdir(parents=True)
        for name, body in files.items():
            (folder/name).write_bytes(body)
        return await call(runner, attempt, "dag_update", {"source_dir": ".private/dags/" + key,
            "expected_revision": expected, "summary": "Add independent planning nodes."}, key)
    first = await save(pair("A"), 0, "a")
    second = await save(pair("B"), 0, "b")
    assert second["revision"] == 2
    assert {n["id"] for n in json.loads(Path(second["definitions"]).read_bytes())["nodes"]} == {"A", "B"}
    # Both branches inserted the same new ID with different contents.
    conflict = pair("A")
    conflict["definitions.json"] = conflict["definitions.json"].replace(b'"title": "A"', b'"title": "changed"')
    with pytest.raises(HubError) as failure:
        await save(conflict, 0, "conflict")
    assert failure.value.code == "DAG_REVISION_CONFLICT"
    assert json.loads((hub.root/"graphs/current.json").read_bytes())["revision"] == 2
    assert Path(first["definitions"]).is_file()
