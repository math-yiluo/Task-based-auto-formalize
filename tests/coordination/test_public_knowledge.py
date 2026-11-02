"""Regression specifications for the document path; pending execution."""
from types import SimpleNamespace

import pytest

from formal_geyser.batch_validation import validate_batch
from formal_geyser.coordination.knowledge import documentation_report
from formal_geyser.coordination.models import ValidationReport
from formal_geyser.coordination.storage import BlobStore


def snapshot(blobs, files):
    return blobs.put_json({"files": [{"path": path, "sha256": blobs.put(body.encode())}
                                     for path, body in sorted(files.items())]})


@pytest.fixture
def source(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    files = {"Example.lean": "def exampleValue : Nat := 1\n", "Example.md": "An example.\n",
             "wiki/README.md": "# Knowledge\n## Essential\nShort guidance.\n## Reference notes\n"}
    return blobs, files, snapshot(blobs, files)


def test_public_note_diff_retains_exact_binding_without_lean(source):
    blobs, files, base = source
    final = snapshot(blobs, {**files, "notes/lesson.md": "# Lesson\nA useful observation.\n"})
    report = documentation_report(blobs, base, final)
    assert report["verdict"] == "accepted"
    assert report["details"]["integration_snapshot"] == final
    assert report["details"]["base_snapshot"] == base
    assert report["details"]["validation_method"] == "public-documentation"
    assert report["details"]["proof_accepted"] is False


@pytest.mark.parametrize("path", ["Example.lean", "Example.md", "lakefile.lean", "lean-toolchain", "other.md"])
def test_other_changes_never_take_the_document_shortcut(source, path):
    blobs, files, base = source
    assert documentation_report(blobs, base, snapshot(blobs, {**files, path: "Changed\n"})) is None


def test_paired_markdown_even_under_notes_requires_domain_validation(source):
    blobs, files, _ = source
    files.update({"notes/Result.lean": "def result : Prop := True\n", "notes/Result.md": "A proposition.\n"})
    base = snapshot(blobs, files)
    final = snapshot(blobs, {**files, "notes/Result.md": "A changed description.\n"})
    assert documentation_report(blobs, base, final) is None


@pytest.mark.parametrize("path", ["wiki/README.md", "notes/empty.md"])
def test_empty_public_prose_is_reported(source, path):
    blobs, files, base = source
    report = documentation_report(blobs, base, snapshot(blobs, {**files, path: "  \n"}))
    assert report["verdict"] == "rejected" and path in report["details"]["reason"]


def test_no_new_readme_schema_or_link_parser(source):
    blobs, files, base = source
    final = snapshot(blobs, {**files, "wiki/README.md": "# Short project instructions\nRead relevant notes as needed.\n"})
    assert documentation_report(blobs, base, final)["verdict"] == "accepted"


async def test_document_batch_skips_domain_build_but_labels_cannot_bypass_it(source):
    blobs, files, base = source
    calls = []
    class Plugin:
        async def validate_final_tree(self, hub, items, snapshot_sha):
            calls.append(snapshot_sha)
            return ValidationReport(verdict="accepted", policy="lean.peer@1",
                                    details={"integration_snapshot": snapshot_sha})
    items = [{"artifact": {"id": "note", "kind": "note.wiki"},
              "task": {"contract": {"acceptance_policy": "lean.peer@1"}}}]
    hub = SimpleNamespace(blobs=blobs)
    doc = snapshot(blobs, {**files, "notes/topic.md": "# Topic\nReusable material.\n"})
    result = await validate_batch(Plugin(), hub, items, doc, base_snapshot=base)
    assert calls == [] and result["note"]["policy"] == "lean.peer@1"
    changed = snapshot(blobs, {**files, "Example.lean": "def exampleValue : Nat := 2\n"})
    await validate_batch(Plugin(), hub, items, changed, base_snapshot=base)
    assert calls == [changed]
