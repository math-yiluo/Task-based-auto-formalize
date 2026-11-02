"""Native archive integrity and crash recovery; these are storage fixtures only."""
from __future__ import annotations

import json
import stat

import pytest

from formal_geyser.coordination import artifact_files
from formal_geyser.coordination.artifact_files import ArtifactFiles
from formal_geyser.coordination.content import ContentError, NativeContentStore
from formal_geyser.coordination.storage import digest


@pytest.fixture
def staged(tmp_path):
    store = ArtifactFiles(NativeContentStore(tmp_path / ".task-based"))
    data = {"version": 2, "name": "Archive storage fixture", "bundle": {}}
    sources = {"Fixture.lean": b"-- Storage fixture only.\n", "Fixture.md": b"Storage test.\n"}
    path = store.stage("candidate", data, sources)
    return store, data, sources, path


def test_archive_preserves_submitted_final_and_publication_receipt(staged):
    store, data, sources, path = staged
    assert path == store.root / "staging/candidate/2/record.json"
    receipt = {"disposition": "published", "commit": "a" * 40, "independent_review": "review-1"}
    final = {"final/Fixture.lean": b"-- Integrated storage fixture.\n", "final/Fixture.md": b"Integrated test.\n"}
    archived = store.relocate(path.parent, "candidate", data, "artifacts", receipt, final)
    assert archived == store.root / "artifacts/candidate/2/record.json"
    assert not path.exists()
    assert store.resolve(path, "candidate", digest(data)) == archived
    for name, body in sources.items():
        assert (archived.parent / "files" / name).read_bytes() == body
    assert json.loads((archived.parent / "publication.json").read_bytes()) == receipt
    assert (archived.parent / "final/Fixture.lean").read_bytes() == final["final/Fixture.lean"]
    library = store.root / "library"
    library.mkdir()
    current = library / "Fixture.lean"
    current.write_bytes(final["final/Fixture.lean"])
    current.unlink()
    store.verify(archived.parent, "candidate", 2, digest(data))


@pytest.mark.parametrize("outcome", ["rejected", "withdrawn"])
def test_nonaccepted_submissions_retain_reason_outside_accepted_archive(staged, outcome):
    store, data, sources, path = staged
    archived = store.relocate(path.parent, "candidate", data, "submission-history",
                              {"disposition": outcome, "reason": "Concrete storage-test disposition."})
    assert archived.is_relative_to(store.root / "submission-history")
    assert not (store.root / "artifacts").exists()
    assert json.loads((archived.parent / "history.json").read_bytes())["disposition"] == outcome
    assert (archived.parent / "files/Fixture.lean").read_bytes() == sources["Fixture.lean"]


def test_a_move_finished_by_another_thread_meanwhile_is_not_repeated(staged):
    store, data, _, path = staged
    receipt = {"disposition": "rejected", "reason": "Concrete storage-test disposition."}
    archived = store.relocate(path.parent, "candidate", data, "submission-history", receipt)
    # A second caller planned the same move from the old folder before the first finished.
    assert store.relocate(path.parent, "candidate", data, "submission-history", receipt) == archived
    assert json.loads((archived.parent / "history.json").read_bytes()) == receipt


def test_interrupted_relocation_resolves_after_restart_and_repairs_old_marker(staged, monkeypatch):
    store, data, _, path = staged
    original = artifact_files._write_new
    def fail_marker(target, body, **options):
        if target.name == "relocated.json":
            raise OSError("Interrupted between relocation and marker")
        original(target, body, **options)
    monkeypatch.setattr(artifact_files, "_write_new", fail_marker)
    with pytest.raises(OSError, match="Interrupted"):
        store.relocate(path.parent, "candidate", data, "artifacts", {"commit": "a" * 40})
    restarted = ArtifactFiles(NativeContentStore(store.content.state_dir))
    archived = restarted.resolve(path, "candidate", digest(data))
    restarted.verify(archived.parent, "candidate", 2, digest(data))
    monkeypatch.setattr(artifact_files, "_write_new", original)
    assert restarted.relocate(archived.parent, "candidate", data, "artifacts", {"commit": "a" * 40}) == archived
    assert (path.parent / "relocated.json").is_file()


def test_altered_frozen_files_never_repair_from_snapshot_or_relocate(staged):
    store, data, _, path = staged
    lean = path.parent / "files/Fixture.lean"
    lean.chmod(stat.S_IREAD | stat.S_IWRITE)
    lean.write_bytes(b"changed")
    with pytest.raises(ContentError, match="integrity"):
        store.relocate(path.parent, "candidate", data, "artifacts", {"commit": "a" * 40})
    assert lean.read_bytes() == b"changed"
    assert not (store.root / "artifacts").exists()
