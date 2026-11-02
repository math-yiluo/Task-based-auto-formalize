import hashlib
import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from formal_geyser.coordination import library_metrics
from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.library_metrics import (
    SAVED_COUNT,
    LibraryMetrics,
    seconds_to_next_hour,
)
from formal_geyser.coordination.models import ProjectConfig


def publish(root, commit, files):
    tree = root / "library" / commit
    entries = []
    for name, body in files.items():
        path = tree / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        entries.append({"path": name, "bytes": len(body), "commit": commit,
                        "sha256": hashlib.sha256(body).hexdigest()})
    manifest = json.dumps({"schema_version": 1, "kind": "public", "commit": commit,
                           "files": entries}).encode()
    (tree / "manifest.json").write_bytes(manifest)
    (root / "library/current.json").write_text(json.dumps({
        "schema_version": 1, "commit": commit, "root": f"{commit}/files",
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }))


def recording_reads(monkeypatch):
    reads = []
    original = library_metrics._read

    def read(path):
        reads.append(path.name)
        return original(path)

    monkeypatch.setattr(library_metrics, "_read", read)
    return reads


def test_only_latest_published_sources_count_with_physical_line_semantics(tmp_path):
    publish(tmp_path, "a" * 40, {"Historical.lean": b"old\n" * 100})
    publish(tmp_path, "b" * 40, {
        "Langlands/A.lean": b"def a := 1\r\n\r\n-- comment\r\n",
        "Langlands/B.lean": b"", "Langlands/C.lean": b"def c := 2",
        "Langlands/C.md": b"documentation\n" * 30,
    })
    for folder in ["staging", "drafts", ".dependencies/Mathlib"]:
        path = tmp_path / folder / "NotPublished.lean"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ignored\n" * 100)
    assert LibraryMetrics(tmp_path, clock=lambda: 1000.0).count() == {
        "commit": "b" * 40, "lean_files": 3, "lean_lines": 4, "counted_at": 1_000_000,
    }


def test_unchanged_content_is_not_reread_and_removed_files_stop_counting(tmp_path, monkeypatch):
    publish(tmp_path, "a" * 40, {"A.lean": b"a\n", "B.lean": b"b\n\n"})
    metrics = LibraryMetrics(tmp_path, clock=lambda: 1.0)
    assert metrics.count()["lean_lines"] == 3
    reads = recording_reads(monkeypatch)
    assert metrics.count()["lean_lines"] == 3
    assert reads == ["current.json"]
    publish(tmp_path, "b" * 40, {"A.lean": b"a\n", "C.lean": b"c"})
    reads.clear()
    assert metrics.count() == {"commit": "b" * 40, "lean_files": 2, "lean_lines": 2, "counted_at": 1000}
    assert reads == ["current.json", "manifest.json", "C.lean"]


def test_a_restart_serves_the_saved_count_and_counts_only_new_sources(tmp_path, monkeypatch):
    # The pilot's first count after a restart read all 5,017 Lean files and took over 30 s.
    publish(tmp_path, "a" * 40, {"A.lean": b"a\n", "B.lean": b"b\n\n"})
    first = LibraryMetrics(tmp_path, clock=lambda: 7.0).count()
    reads = recording_reads(monkeypatch)
    restarted = LibraryMetrics(tmp_path, clock=lambda: 9.0)
    assert restarted.latest() == first and reads == []
    publish(tmp_path, "b" * 40, {"A.lean": b"a\n", "B.lean": b"b\n\n", "C.lean": b"c\n"})
    assert restarted.latest() == first  # Serving never counts.
    assert restarted.count() == {"commit": "b" * 40, "lean_files": 3, "lean_lines": 4, "counted_at": 9000}
    assert reads == ["current.json", "manifest.json", "C.lean"]


def test_a_damaged_saved_count_is_ignored(tmp_path):
    publish(tmp_path, "a" * 40, {"A.lean": b"a\n"})
    saved = tmp_path / SAVED_COUNT
    saved.parent.mkdir(parents=True)
    for body in [b"not JSON", json.dumps({"schema_version": 1, "commit": "a" * 40, "lean_files": -1}).encode(),
                 json.dumps({"schema_version": 2}).encode()]:
        saved.write_bytes(body)
        assert LibraryMetrics(tmp_path).latest() is None
    assert LibraryMetrics(tmp_path, clock=lambda: 2.0).count()["lean_lines"] == 1
    assert LibraryMetrics(tmp_path).latest()["counted_at"] == 2000


def test_bad_source_or_navigation_does_not_return_a_misleading_count(tmp_path):
    publish(tmp_path, "a" * 40, {"A.lean": b"a\n"})
    (tmp_path / "library" / ("a" * 40) / "files/A.lean").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="source does not match"):
        LibraryMetrics(tmp_path).count()
    (tmp_path / "library/current.json").write_text(json.dumps({
        "schema_version": 1, "commit": "a" * 40, "root": "../../private",
    }))
    with pytest.raises(ValueError, match="navigation"):
        LibraryMetrics(tmp_path).count()


def test_a_failed_count_keeps_the_last_one_marked_with_its_error(tmp_path):
    publish(tmp_path, "a" * 40, {"A.lean": b"a\n"})
    metrics = LibraryMetrics(tmp_path, clock=lambda: 3.0)
    assert metrics.refresh()["lean_lines"] == 1
    (tmp_path / "library/current.json").write_text("not JSON")
    assert metrics.refresh() is None
    assert metrics.latest()["lean_lines"] == 1 and metrics.latest()["error"]
    publish(tmp_path, "b" * 40, {"A.lean": b"a\n", "B.lean": b"b\n"})
    assert metrics.refresh()["lean_lines"] == 2 and "error" not in metrics.latest()


def test_counts_fall_on_the_hour():
    on_the_hour = datetime(2026, 10, 30, 10, 0, 0).timestamp()
    assert seconds_to_next_hour(on_the_hour - 30) == pytest.approx(30)
    assert seconds_to_next_hour(on_the_hour) == pytest.approx(3600)
    assert seconds_to_next_hour(on_the_hour + 0.5) == pytest.approx(3599.5)


def stats_when_counted(client):
    deadline = time.monotonic() + 10
    while (response := client.get("/v1/library/stats")).status_code == 503 and time.monotonic() < deadline:
        time.sleep(0.05)
    return response


def test_operator_stats_serve_the_hourly_count_and_fail_separately(tmp_path):
    initialize(tmp_path, ProjectConfig())
    with TestClient(create_app(tmp_path)) as client:
        # Nothing is saved, so the service counts once as it starts.
        response = stats_when_counted(client)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["commit"] is None and response.json()["lean_lines"] == 0
        publish(tmp_path, "a" * 40, {"A.lean": b"a\n"})
        assert client.get("/v1/library/stats").json()["lean_lines"] == 0  # A request never counts.
        client.app.state.library_metrics.refresh()  # The hourly count.
        assert client.get("/v1/library/stats").json()["lean_lines"] == 1
        (tmp_path / "library/current.json").write_text("not JSON")
        client.app.state.library_metrics.refresh()
        stale = client.get("/v1/library/stats").json()
        assert stale["lean_lines"] == 1 and stale["error"]
        assert client.get("/v1/runs").json() == []
    saved = json.loads((tmp_path / SAVED_COUNT).read_text())["counted_at"]
    with TestClient(create_app(tmp_path)) as client:
        # A restart serves the saved count without counting again.
        assert client.get("/v1/library/stats").json()["counted_at"] == saved


def test_stats_are_unavailable_until_a_first_count_succeeds(tmp_path):
    initialize(tmp_path, ProjectConfig())
    (tmp_path / "library").mkdir(exist_ok=True)
    (tmp_path / "library/current.json").write_text("not JSON")
    with TestClient(create_app(tmp_path)) as client:
        deadline = time.monotonic() + 10
        while not client.app.state.library_metrics.error and time.monotonic() < deadline:
            time.sleep(0.05)
        response = client.get("/v1/library/stats")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "LIBRARY_STATS_UNAVAILABLE"
        assert "not been counted yet" in response.json()["error"]["message"]
