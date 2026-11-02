"""Per-operation native verification of immutable artifact bodies."""
from copy import deepcopy

import pytest

from formal_geyser.coordination.artifact_files import ArtifactFiles
from formal_geyser.coordination.errors import HubError
from tests.coordination.test_archive_publication import archive_case as archive_case


async def test_repeated_dependency_reads_share_checks_and_the_next_operation_rechecks_the_pair(archive_case, monkeypatch):
    case = archive_case
    count = []
    original = ArtifactFiles.verify
    def verify(self, *args, **kwargs):
        count.append(args)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ArtifactFiles, "verify", verify)
    def read(c):
        record = case.hub._get(c, "artifacts", case.candidate["artifact_id"])
        first = case.hub._record_body(c, record)
        expected = deepcopy(first)
        first["name"] = "A caller's local change must not mutate the cached immutable body"
        assert case.hub._record_body(c, record) == expected
    await case.hub.db.call(read)
    # The archive's manifest is the pinned one: no operation re-reads every archived file.
    assert count == []
    await case.hub.db.call(read)
    assert count == []
    artifact = await case.hub.artifact(case.candidate["artifact_id"])
    from pathlib import Path
    path = Path(artifact["content_file"]["root"]) / "files" / artifact["data"]["bundle"]["lean_path"]
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"-- Changed after the checked operation.\n")
    with pytest.raises((HubError, ValueError)):
        await case.hub.db.call(read)
