"""Exact module closure, checker guards, and configured source-size regressions."""
from copy import deepcopy

import pytest

from formal_geyser import schemas
from formal_geyser.coordination.snapshot_limits import (
    FORMER_MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_FILES,
)
from formal_geyser.coordination.storage import BlobStore
from formal_geyser.mathematical_artifacts import (
    module_imports,
    snapshot_module_order,
    source_closure,
    validate_snapshot_sources,
)
from formal_geyser.plugin import LeanPlugin
from formal_geyser.registry import RegistryError
from formal_geyser.schemas import ExecutionLimits


def paired_sources(sources):
    return {path: body for name, source in sources.items()
            for path, body in ((name + ".lean", source.encode()), (name + ".md", (name + " explanation").encode()))}


@pytest.mark.parametrize("source,expected", [
    ("import\n Foo\ndef x := 1", {"Init", "Foo"}),
    ("import /- outer\n /- nested -/ -/ Foo\nimport\tBar", {"Init", "Foo", "Bar"}),
    ("module\npublic import Foo\nmeta import Bar\nimport all Baz", {"Init", "Foo", "Bar", "Baz"}),
    ("module\nprelude\npublic meta import Foo", {"Foo"}),
    ("import «Foo».«Bar»\nimport«Baz»", {"Init", "Foo.Bar", "Baz"}),
    ("-- import Ignore\n/- import Ignore -/\ndef x := \"import Ignore\"", {"Init"}),
])
def test_portable_header_grammar(source, expected):
    assert module_imports(source) == expected


@pytest.mark.parametrize("source", ["import Α", "import «Foo.Bar»", "import Foo.", "import Fooα",
                                     "import /- unterminated", "private import Foo", "public import Foo",
                                     "module\npublic import all Foo"])
def test_unsupported_or_ambiguous_import_headers_fail_closed(source):
    with pytest.raises(RegistryError):
        module_imports(source)


def test_module_order_handles_deep_graph_without_python_recursion(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    files = paired_sources({f"M{index:04}": (f"import M{index + 1:04}\n" if index < 1099 else "") + f"def value{index} : Nat := 0"
                            for index in range(1100)})
    entries = [{"path": path, "sha256": blobs.put(body)} for path, body in sorted(files.items())]
    assert snapshot_module_order(blobs, entries) == [f"M{index:04}" for index in reversed(range(1100))]


def test_toolchain_module_shadowing_is_rejected(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    files = paired_sources({"Init": "prelude\ndef replacement : Nat := 0"})
    snapshot = blobs.put_json({"files": [{"path": path, "sha256": blobs.put(body)} for path, body in files.items()]})
    with pytest.raises(RegistryError, match="shadow"):
        validate_snapshot_sources(blobs, snapshot)


def test_source_closure_follows_imports_and_excludes_unrelated_modules(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    files = paired_sources({"Main": "import Helper\n/- import Unrelated -/\ndef target : Prop := True",
                            "Helper": "import Leaf\ndef helper : Nat := leaf", "Leaf": "def leaf : Nat := 1",
                            "Unrelated": "def unrelated : Nat := 2"})
    entries = [{"path": path, "sha256": blobs.put(body)} for path, body in sorted(files.items())]
    closure = source_closure(blobs, entries, ["Main.lean"])
    assert {item["path"] for item in closure} == {name + ext for name in ("Main", "Helper", "Leaf") for ext in (".lean", ".md")}
    changed = [{**item, "sha256": blobs.put(b"def unrelated : Nat := 3")} if item["path"] == "Unrelated.lean" else item for item in entries]
    assert source_closure(blobs, changed, ["Main.lean"]) == closure




def configured_size_case(proof_case):
    """A definition task whose snapshot holds 2,050 files."""
    hub, original_task, _ = proof_case
    files = paired_sources({f"M{index:04}": f"def value{index} : Nat := {index}\n" for index in range(1025)})
    snapshot = hub.blobs.put_json({"files": [{"path": path, "sha256": hub.blobs.put(body)} for path, body in sorted(files.items())]})
    task = deepcopy(original_task)
    task["contract"].update(acceptance_policy="lean.definition@1", inputs=["environment-1"], payload={"environment_artifact": "environment-1"})
    payload = {"kind": "lean.definition", "data": {"name": "Configured-size fixture", "description": "Synthetic normalization fixture.",
        "entry_module": "M0000", "declaration": "value0", "provenance": "internal", "provenance_rationale": "Source-size regression fixture."}}
    return hub, task, payload, snapshot, hub.artifacts["environment-1"]["data"]


async def test_normalization_honors_configured_snapshot_limit(proof_case):
    hub, task, payload, snapshot, environment = configured_size_case(proof_case)
    environment["limits"]["snapshot_files"] = 4096
    normalized = await LeanPlugin().normalize_submission(hub, task, payload, snapshot)
    assert normalized["data"]["bundle"]["snapshot"] == snapshot
    environment["limits"]["snapshot_files"] = 2048
    with pytest.raises(RegistryError, match="Snapshot file limit"):
        await LeanPlugin().normalize_submission(hub, task, payload, snapshot)


async def test_an_environment_at_the_former_cap_allows_the_current_cap(proof_case, monkeypatch):
    # Every task pins its environment, and the pilot's was recorded at the former platform cap of
    # 20,000 files, which candidates holding the whole Library outgrew. A lower limit still binds.
    assert ExecutionLimits(snapshot_files=FORMER_MAX_SNAPSHOT_FILES).snapshot_file_limit == MAX_SNAPSHOT_FILES
    assert ExecutionLimits(snapshot_files=4096).snapshot_file_limit == 4096
    hub, task, payload, snapshot, environment = configured_size_case(proof_case)
    monkeypatch.setattr(schemas, "FORMER_MAX_SNAPSHOT_FILES", 1024)
    environment["limits"]["snapshot_files"] = 1024
    normalized = await LeanPlugin().normalize_submission(hub, task, payload, snapshot)
    assert normalized["data"]["bundle"]["snapshot"] == snapshot
    environment["limits"]["snapshot_files"] = 1023
    with pytest.raises(RegistryError, match="Snapshot file limit"):
        await LeanPlugin().normalize_submission(hub, task, payload, snapshot)
