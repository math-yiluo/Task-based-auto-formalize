"""Exact compressed report storage, independent of orchestration."""
import json

import pytest

from formal_geyser.coordination.reports import ReportStore


def test_large_reports_are_exact_deduplicated_and_keep_negative_quiescence(tmp_path):
    store = ReportStore(tmp_path)
    details = {"integration_snapshot": "a" * 64, "proof_accepted": False,
               "compilation": {"process": {"stdout": "log line\n" * 100000, "tree_quiet": False}}}
    one, two = store.compact(details), store.compact(details)
    assert one == two and len(json.dumps(one)) < 4096
    assert not one["process_quiet"] and one["integration_snapshot"] == details["integration_snapshot"]
    assert store.read(one["report_file"]["sha256"]) == details
    assert list(tmp_path.rglob("*.json.gz")) == [store.path(one["report_file"]["sha256"])]
    assert store.compact(one) == one
    store.path(one["report_file"]["sha256"]).write_bytes(b"damaged")
    with pytest.raises((OSError, ValueError)):
        store.compact(one)



def test_report_references_are_portable_across_backup_roots(tmp_path):
    one, two = ReportStore(tmp_path / "original"), ReportStore(tmp_path / "restored")
    body = {"reason": "Unavailable", "stdout": "original output\n" * 1000}
    assert one.compact(body) == two.compact(body)
