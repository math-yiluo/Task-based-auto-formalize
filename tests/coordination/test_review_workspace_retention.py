"""Retention never deletes unrecorded work or its immutable evidence."""
from tests.coordination.test_repository import freeze, manifest, workspace
from tests.coordination.test_repository import store as store


def test_old_finalized_checkout_can_be_reclaimed_without_losing_snapshot(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    final = freeze(store, tmp_path, "finalized", {"Example.lean": b"def x := 1", "Example.md": b"One."}, base)
    location = workspace(tmp_path, "finalized")
    result = repository.prune_integration_workspace(location, blobs)
    assert result["state"] == "reclaimed" and result["bytes"] > 0
    assert not location.exists()
    assert blobs.get_json(final["snapshot"])["files"]
    assert repository.snapshot(final["commit"], blobs) == final["snapshot"]
    assert repository.prune_integration_workspace(location, blobs)["state"] == "already_absent"


def test_modified_or_unfrozen_checkout_is_preserved(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    location = workspace(tmp_path, "unfinished")
    repository.prepare(blobs, manifest(blobs, {"Work.md": b"first"}), base, location, ["Work.md"])
    assert repository.prune_integration_workspace(location, blobs)["state"] == "retained_unfrozen"
    repository.finalize(location, blobs)
    (location / "Work.md").write_text("unsaved follow-up", encoding="utf-8")
    assert repository.prune_integration_workspace(location, blobs)["state"] == "retained_modified"
    assert (location / "Work.md").read_text() == "unsaved follow-up"


def test_private_extra_files_are_never_discarded(store, tmp_path):
    repository, blobs = store
    base = repository.initialize(blobs)["head"]
    freeze(store, tmp_path, "private", {"Work.md": b"first"}, base)
    location = workspace(tmp_path, "private")
    (location / ".private").mkdir()
    (location / ".private" / "scratch.md").write_text("Keep this.", encoding="utf-8")
    assert repository.prune_integration_workspace(location, blobs)["state"] == "retained_extra_files"
    assert location.exists()
