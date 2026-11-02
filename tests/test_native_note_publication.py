"""Source-selection boundaries between shared notes and mathematical candidates."""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    Command,
    ProjectConfig,
    RepositoryConfig,
    Role,
    TaskContract,
)
from formal_geyser.plugin import LeanPlugin
from formal_geyser.publication import validate_note_publication_paths
from formal_geyser.registry import RegistryError


def snapshot(hub, files):
    return hub.blobs.put_json({"files": [
        {"path": path, "sha256": hub.blobs.put(body)} for path, body in sorted(files.items())]})


def note_task():
    contract = TaskContract(title="Review a shared note", objective="Review synthetic source metadata.",
                            domain="lean", acceptance_policy="lean.peer@1")
    return {"id": "synthetic-note-task", "revision": 1, "contract": contract.model_dump(mode="json")}


@pytest.fixture
async def public_tree(tmp_path):
    hub = Hub(tmp_path, config=ProjectConfig(domain="lean", repository=RepositoryConfig(enabled=True)))
    files = {"Math/Main.lean": b"def synthetic : Prop := True\n",
             "Math/Main.md": b"# Synthetic statement fixture\n",
             "wiki/Start.md": b"# Existing shared note\n"}
    try:
        base = await hub.execute(Command(name="repository.initialize", key=uuid4().hex,
            payload={"snapshot": snapshot(hub, files)}), Actor(role=Role.OPERATOR))
        yield hub, base["head"], files
    finally:
        await hub.close()


@pytest.mark.parametrize("path,action", [
    ("Math/Main.lean", "change"), ("Math/Main.lean", "delete"),
    ("Math/Main.md", "change"), ("Math/Main.md", "delete"),
    ("Fresh.lean", "add"), ("Fresh.md", "add"),
])
async def test_note_selected_changes_cannot_publish_mathematical_source(public_tree, path, action):
    hub, base, original = public_tree
    files = dict(original)
    if action == "delete":
        files.pop(path)
    elif action == "change":
        files[path] += b"\n-- Changed synthetic fixture\n"
    else:
        files.update({"Fresh.lean": b"def fresh : Prop := True\n", "Fresh.md": b"# Fresh fixture\n"})
    payload = {"kind": "research.note", "data": {"summary": "Ordinary note"}, "repository_paths": [path]}
    with pytest.raises(RegistryError, match="Notes cannot publish changed Lean"):
        LeanPlugin().validate_publication_paths(hub, note_task(), payload, snapshot(hub, files), base, [path])


async def test_note_allows_unselected_private_build_evidence_and_unchanged_library_inputs(public_tree):
    hub, base, original = public_tree
    files = {**original, "PrivateCheck.lean": b"def checked : Prop := True\n",
             "PrivateCheck.md": b"# Private build evidence\n", "wiki/Start.md": b"# Updated note\n",
             "sources/input.pdf": b"%PDF-1.7 synthetic metadata", "metadata.json": b'{"summary":"note"}'}
    sha = snapshot(hub, files)
    payload = {"kind": "research.note", "data": {"summary": "Ordinary note"}}
    plugin = LeanPlugin()
    # Pairing/hole scans remain active even though these files are not selected.
    plugin.validate_candidate_snapshot(hub, note_task(), payload, sha)
    plugin.validate_publication_paths(hub, note_task(), payload, sha, base,
        ["Math/Main.lean", "Math/Main.md", "wiki/Start.md", "sources/input.pdf", "metadata.json"])


async def test_selected_markdown_is_protected_by_companion_in_base_even_if_candidate_omits_it(public_tree):
    hub, base, _ = public_tree
    sha = snapshot(hub, {"Math/Main.md": b"Rewritten source explanation\n"})
    with pytest.raises(RegistryError, match="associated Markdown"):
        LeanPlugin().validate_publication_paths(hub, note_task(),
            {"kind": "research.note", "data": {}}, sha, base, ["Math/Main.md"])


async def test_source_pdf_notes_are_not_blanket_banned(public_tree):
    hub, base, original = public_tree
    body = b"%PDF-1.7 synthetic source metadata"
    sha = snapshot(hub, {**original, "sources/input.pdf": body})
    LeanPlugin().validate_publication_paths(hub, note_task(),
        {"kind": "research.note", "data": {"summary": "A supplied PDF accompanies this source note."}},
        sha, base, ["sources/input.pdf"])


def test_build_candidate_keeps_private_validation_snapshot_without_inferred_publication(build_case):
    hub, task, artifact = build_case
    plugin = LeanPlugin()
    assert plugin.publication_paths(task, artifact) == []
    plugin.validate_candidate_snapshot(hub, task, artifact, artifact["snapshot"])
    plugin.validate_publication_paths(hub, task, artifact, artifact["snapshot"], None, [])
    with pytest.raises(RegistryError, match="Notes cannot publish changed Lean"):
        plugin.validate_publication_paths(hub, task, artifact, artifact["snapshot"], None,
                                          ["Candidate.lean", "Candidate.md"])


def test_math_kind_alone_cannot_exempt_a_generic_note_from_the_guard(build_case):
    hub, task, artifact = build_case
    with pytest.raises(ValidationError):
        LeanPlugin().validate_publication_paths(hub, task,
            {"kind": "lean.definition", "data": {"summary": "Not a mathematical schema"}},
            artifact["snapshot"], None, ["Candidate.lean"])


def test_genuine_mathematical_candidates_retain_their_existing_publication_paths(proof_case):
    hub, task, artifact = proof_case
    plugin = LeanPlugin()
    assert plugin.publication_paths(task, artifact) == ["Candidate.lean", "Candidate.md"]
    plugin.validate_candidate_snapshot(hub, task, artifact, artifact["snapshot"])
    plugin.validate_publication_paths(hub, task, artifact, artifact["snapshot"], None,
                                      ["Candidate.lean", "Candidate.md"])
    changed = deepcopy(artifact)
    changed["data"]["bundle"]["lean_sha256"] = "0" * 64
    with pytest.raises(RegistryError, match="path/hash"):
        plugin.validate_candidate_snapshot(hub, task, changed, artifact["snapshot"])


@pytest.mark.parametrize("path", ["Main.LEAN", "Main.MD", "main.md"])
def test_note_guard_treats_case_aliases_of_sources_as_protected(path):
    baseline = {"Main.lean": "a" * 64, "Main.md": "b" * 64}
    candidate = {**baseline, path: "c" * 64}
    with pytest.raises(RegistryError, match="associated Markdown"):
        validate_note_publication_paths([path], candidate, baseline)
