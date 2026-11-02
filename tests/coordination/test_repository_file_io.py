"""Long local run paths must not prevent reviewer workspace preparation."""
from __future__ import annotations

import pytest

from formal_geyser.coordination.repository import RepositoryError, RepositoryStore


def test_atomic_review_workspace_file_install_uses_long_path_io(tmp_path):
    root = tmp_path
    while len(str(root)) < 275:
        root = root / "nested-review-workspace"
    root.mkdir(parents=True)
    path = root / "alternates"
    RepositoryStore._install_workspace_file(path, b"Exact local object store reference.\n")
    assert path.read_bytes() == b"Exact local object store reference.\n"
    RepositoryStore._install_workspace_file(path, b"Exact local object store reference.\n")
    with pytest.raises(RepositoryError, match="unexpected files"):
        RepositoryStore._install_workspace_file(path, b"Changed reference.\n")
