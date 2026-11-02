"""Synthetic language/protocol fixtures; no domain workload or model is executed."""
from __future__ import annotations

import hashlib
import stat
import threading
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from formal_geyser import mathematical_artifacts
from formal_geyser.coordination.models import TaskContract
from formal_geyser.mathematical_artifacts import (
    lean_tokens,
    snapshot_module_order,
    validate_snapshot_sources,
)
from formal_geyser.plugin import LeanPlugin
from formal_geyser.publication import publication_policy, validate_metadata
from formal_geyser.registry import (
    ObjectRegistry,
    RegistryError,
    read_snapshot,
    snapshot_sources,
)
from formal_geyser.schemas import (
    OBJECT_ADAPTER,
    DefinitionRecord,
    LeanMarkdownBundle,
    SourceLocation,
    StatementRecord,
    stored_record,
)
from tests.conftest import FakeHub, FakeRunner, cite


def files_snapshot(hub, files):
    return hub.blobs.put_json({"files": [{"path": path, "sha256": hub.blobs.put(text.encode())}
                                       for path, text in sorted(files.items())]})


@pytest.mark.parametrize("text", ["by sorry", "by admit", "axiom unchecked : Prop",
                                 "by exact Lean.sorryAx _ false",
                                 "by exact «sorryAx» _ false"])
def test_lexical_holes_cannot_enter_publication_snapshots(tmp_path, text):
    hub = FakeHub(tmp_path)
    sha = files_snapshot(hub, {"Unit.lean": text, "Unit.md": "Software explanation."})
    with pytest.raises(RegistryError, match="prohibited"):
        validate_snapshot_sources(hub.blobs, sha)


def test_nested_comments_literals_raw_strings_and_identifier_boundaries(tmp_path):
    source = '''/- sorry /- axiom -/ admit -/
-- sorryAx
def explanatoryText := "sorry axiom admit"
def rawText := r##"a quote \" sorryAx /-"##
def sorryFree := 'x'
def admitCount := 1
'''
    assert not {"sorry", "admit", "axiom", "sorryAx"}.intersection(lean_tokens(source))
    hub = FakeHub(tmp_path)
    sha = files_snapshot(hub, {"Unit.lean": source, "Unit.md": "Software explanation."})
    assert validate_snapshot_sources(hub.blobs, sha)


def test_legitimate_axioms_variable_passes_source_gate(tmp_path):
    hub = FakeHub(tmp_path)
    sha = files_snapshot(hub, {"Fixture.lean": "def count : Nat := Id.run do\n  let axioms := 1\n  return axioms\n",
                               "Fixture.md": "A software fixture with a local variable named axioms."})
    assert validate_snapshot_sources(hub.blobs, sha)


@pytest.mark.parametrize("stem", ["Inspect", "KernelReplay"])
def test_paired_bundled_helpers_pass_source_gate(tmp_path, stem):
    hub = FakeHub(tmp_path)
    helper = Path(__file__).resolve().parents[1] / "src/formal_geyser/lean_tools"
    sha = files_snapshot(hub, {name: (helper / name).read_text(encoding="utf-8")
                               for name in (stem + ".lean", stem + ".md")})
    assert validate_snapshot_sources(hub.blobs, sha)


@pytest.mark.parametrize("source", ['"unterminated sorry', '/- unterminated', 'r##"unterminated'])
def test_unterminated_lexical_regions_fail_closed(source):
    with pytest.raises(RegistryError, match="Unterminated"):
        lean_tokens(source)


@pytest.mark.parametrize("files", [
    {"Unit.lean": "def unit := 1"},
    {"Unit.lean": "def unit := 1", "Other.md": "Wrong stem."},
    {"Unit.lean": "def unit := 1", "Unit.md": " \n\t"},
])
def test_every_published_lean_file_needs_nonempty_same_stem_markdown(tmp_path, files):
    hub = FakeHub(tmp_path)
    sha = files_snapshot(hub, files)
    with pytest.raises(RegistryError, match="Markdown"):
        LeanPlugin().validate_candidate_snapshot(hub, {}, {"kind": "generic.summary", "data": {}}, sha)


def test_bundle_paths_hashes_and_core_snapshot_are_exact(proof_case):
    hub, task, artifact = proof_case
    plugin = LeanPlugin()
    plugin.validate_candidate_snapshot(hub, task, artifact, artifact["snapshot"])
    assert plugin.publication_paths(task, artifact) == ["Candidate.lean", "Candidate.md"]
    for field in ("lean_sha256", "markdown_sha256", "snapshot"):
        altered = deepcopy(artifact)
        altered["data"]["bundle"][field] = "0" * 64
        with pytest.raises(RegistryError, match="snapshot"):
            plugin.validate_candidate_snapshot(hub, task, altered, artifact["snapshot"])
    with pytest.raises(ValidationError, match="same path stem"):
        LeanMarkdownBundle.model_validate({**artifact["data"]["bundle"], "markdown_path": "Different.md"})


def test_math_definitions_have_no_unpaired_metadata_fallback():
    with pytest.raises(ValidationError):
        DefinitionRecord(name="Old metadata", description="No paired implementation.")


def test_records_staged_in_the_earlier_format_read_as_current_records(proof_case):
    hub, _, artifact = proof_case
    current = deepcopy(artifact["data"])
    legacy = {key: item for key, item in current.items() if key != "statement_declaration"}
    legacy.update(statement_artifact="statement-1", statement_version=1, definitions=[],
                  native_binding={"baseline_declaration": current["statement_declaration"]})
    record = stored_record(legacy)
    # The stored bytes are untouched; the parsed record has only current fields.
    assert record.model_dump(mode="json") == current
    statement = {**hub.artifacts["statement-1"]["data"], "target_type_sha256": "0" * 64, "definitions": []}
    assert stored_record(statement).model_dump(mode="json") == hub.artifacts["statement-1"]["data"]
    # Only a read of the store accepts the earlier format; a new record keeps the strict schema.
    for value in (legacy, statement, {**current, "unexpected": True}):
        with pytest.raises(ValidationError):
            OBJECT_ADAPTER.validate_python(value)
    with pytest.raises(ValidationError):
        stored_record({**current, "unexpected": True})


def test_a_proof_under_another_policy_is_sent_to_a_proof_task(proof_case):
    _, task, artifact = proof_case
    record = OBJECT_ADAPTER.validate_python(artifact["data"])
    contract = TaskContract.model_validate(task["contract"])
    assert publication_policy(contract, record) == "lean.proof@1"
    program = contract.model_copy(update={"acceptance_policy": "lean.peer@1"})
    with pytest.raises(RegistryError, match="lean.peer@1 takes no proof candidates.*lean.proof@1.*not duplicate work"):
        publication_policy(program, record)


def test_source_backed_records_require_exact_provenance(proof_case):
    hub, _, _ = proof_case
    data = deepcopy(hub.artifacts["statement-1"]["data"])
    data.update(provenance="source", provenance_rationale="", sources=[])
    with pytest.raises(ValidationError, match="source references"):
        StatementRecord.model_validate(data)
    data["sources"] = [{"literature_id": "literature-1", "locator": "section"}]
    with pytest.raises(ValidationError, match="content_sha256"):
        StatementRecord.model_validate(data)
    data["sources"] = []
    data["provenance"] = "derived"
    with pytest.raises(ValidationError, match="rationale"):
        StatementRecord.model_validate(data)


async def test_exact_source_versions_are_validated(proof_case):
    hub, _, _ = proof_case
    citation = cite(hub)
    original = StatementRecord.model_validate(hub.artifacts["statement-1"]["data"])
    altered = original.model_copy(update={"sources": [SourceLocation(**{**citation, "content_sha256": "0" * 64})]})
    with pytest.raises(RegistryError, match="fingerprint"):
        await ObjectRegistry(hub).validate_for_publication(altered)


def test_snapshot_module_order_covers_all_sources_and_imports(tmp_path):
    hub = FakeHub(tmp_path)
    sha = files_snapshot(hub, {"Base/Unit.lean": "def x := 1", "Base/Unit.md": "Base.",
                               "Top.lean": "module\n-- import Fake\nimport all Base.Unit\ndef y := 1", "Top.md": "Top.",
                               "Other.lean": "def z := 1", "Other.md": "Other."})
    order = snapshot_module_order(hub.blobs, validate_snapshot_sources(hub.blobs, sha))
    assert set(order) == {"Base.Unit", "Top", "Other"}
    assert order.index("Base.Unit") < order.index("Top")


def test_snapshot_import_cycles_fail_closed(tmp_path):
    hub = FakeHub(tmp_path)
    sha = files_snapshot(hub, {"A.lean": "import B\n", "A.md": "A.", "B.lean": "import A\n", "B.md": "B."})
    with pytest.raises(RegistryError, match="cycle"):
        snapshot_module_order(hub.blobs, validate_snapshot_sources(hub.blobs, sha))


def test_private_integration_sandboxes_are_excluded_from_worker_snapshots(tmp_path):
    hub = FakeHub(tmp_path)
    workspace = tmp_path / "worker"
    integration = workspace / ".taskhub-integration" / "review-1"
    integration.mkdir(parents=True)
    (workspace / "private.txt").write_text("private", encoding="utf-8")
    (integration / "nested.txt").write_text("integration", encoding="utf-8")
    regular = hub.blobs.get_json(snapshot_sources(hub, workspace))
    leaf = hub.blobs.get_json(snapshot_sources(hub, integration))
    assert [entry["path"] for entry in regular["files"]] == ["private.txt"]
    assert [entry["path"] for entry in leaf["files"]] == ["nested.txt"]








async def test_legacy_definition_remains_inspectable_without_new_acceptance(tmp_path):
    hub = FakeHub(tmp_path)
    historical = {"object_type": "definition", "name": "Historical metadata", "description": "Unpaired historical record."}
    hub.artifacts["old"] = {"id": "old", "kind": "lean.definition", "data": historical,
                            "status": "candidate", "accepted": False, "input_refs": []}
    visible = await ObjectRegistry(hub).inspect("old")
    assert visible["data"] == historical
    with pytest.raises(ValidationError):
        await ObjectRegistry(hub).get("old", "definition", approved=True)


class MetadataRunner(FakeRunner):
    """Synthetic checks whose inspector reports a definition of type `Prop` whose value is a
    proposition, with `mutation` applied to its response."""
    metadata = {"kind": "definition", "statement_returns_prop": True, "type_repr": "Prop",
                "statement_target_repr": "synthetic type metadata"}

    def __init__(self, mutation=None):
        super().__init__(lambda response, _: response.update({**self.metadata, **(mutation or {})}))


@pytest.mark.parametrize("mutation,verdict", [({}, "accepted"), ({"kind": "theorem"}, "rejected"),
                                             ({"statement_returns_prop": False}, "rejected")])
async def test_statement_gate_uses_definition_value_not_prop_type(proof_case, lean_service, mutation, verdict):
    hub, _, _ = proof_case
    hub.artifacts["environment-1"]["data"]["local_execution"] = "trusted-code-only"
    lean_service.runner = MetadataRunner(mutation)
    artifact = deepcopy(hub.artifacts["statement-1"])
    task = {"id": "task-1", "revision": 1, "contract": {"acceptance_policy": "lean.statement@1"}}
    report = await validate_metadata(hub, task, artifact)
    assert report.verdict == verdict, report.details
    metadata = report.details["inspection"]["metadata"]
    # The declaration's type is `Prop` every time; only the definition's value decides.
    assert metadata["type_sha256"] == hashlib.sha256(b"Prop").hexdigest()
    assert metadata["statement_returns_prop"] is mutation.get("statement_returns_prop", True)
    if verdict == "accepted":
        assert metadata["statement_target_sha256"] == hashlib.sha256(b"synthetic type metadata").hexdigest()
        assert report.details["checked_modules"] == ["Baseline"]
        assert report.details["correspondence_requirements"] == ["lean-markdown"]
        assert not report.details["proof_accepted"]


def test_snapshot_checks_read_and_tokenize_only_new_files(tmp_path, monkeypatch):
    hub = FakeHub(tmp_path)
    marker = uuid4().hex  # Distinct bytes: passed pairs are remembered for the whole process.
    library = {}
    for number in range(4):
        library[f"Library/Unit{number}.lean"] = f"def unit{number} : Nat := {number} -- {marker}\n"
        library[f"Library/Unit{number}.md"] = f"Unit {number}, {marker}.\n"
    first = files_snapshot(hub, library)
    fresh = {"Fresh.lean": f"def fresh : Nat := 7 -- {marker}\n", "Fresh.md": f"Fresh {marker}.\n"}
    second = files_snapshot(hub, {**library, **fresh})
    tokenized = []
    original = mathematical_artifacts.lean_tokens
    monkeypatch.setattr(mathematical_artifacts, "lean_tokens",
                        lambda source: tokenized.append(source) or original(source))
    assert validate_snapshot_sources(hub.blobs, first)
    assert len(tokenized) == 4
    reads = []
    get = hub.blobs.get
    monkeypatch.setattr(hub.blobs, "get", lambda sha: reads.append(sha) or get(sha))
    assert validate_snapshot_sources(hub.blobs, second)
    assert len(tokenized) == 5  # Only the new Lean file.
    new_blobs = {hashlib.sha256(body.encode()).hexdigest() for body in fresh.values()}
    assert set(reads) == {second, *new_blobs}  # The manifest and the new pair; no unchanged file.
    gap = files_snapshot(hub, {**library, "Gap.lean": f"theorem gap : 1 = 1 := by sorry -- {marker}\n",
                               "Gap.md": "Gap."})
    with pytest.raises(RegistryError, match="prohibited"):
        validate_snapshot_sources(hub.blobs, gap)


def test_a_changed_or_missing_blob_is_read_again(tmp_path):
    hub = FakeHub(tmp_path)
    files = {"Unit.lean": f"def unit : Nat := 1 -- {uuid4().hex}\n", "Unit.md": "Unit."}
    sha = files_snapshot(hub, files)
    assert read_snapshot(hub.blobs, sha)
    lean = hashlib.sha256(files["Unit.lean"].encode()).hexdigest()
    stored = Path(hub.blobs.root) / lean[:2] / lean
    stored.chmod(stat.S_IREAD | stat.S_IWRITE)
    stored.write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        read_snapshot(hub.blobs, sha)
    stored.unlink()
    with pytest.raises(FileNotFoundError):
        read_snapshot(hub.blobs, sha)


async def test_approved_mathematical_reads_check_snapshots_off_the_event_loop(proof_case, monkeypatch):
    hub, _, _ = proof_case
    on_loop = []
    original = mathematical_artifacts.validate_math_record_snapshot

    def observed(*args, **kwargs):
        on_loop.append(threading.current_thread() is threading.main_thread())
        return original(*args, **kwargs)
    monkeypatch.setattr(mathematical_artifacts, "validate_math_record_snapshot", observed)
    await ObjectRegistry(hub).get("statement-1", "statement", approved=True)
    assert on_loop == [False]
