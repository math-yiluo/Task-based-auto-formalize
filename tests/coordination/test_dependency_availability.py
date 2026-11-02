"""Availability follows recorded acceptance only; Library edits never revoke it."""
import json
import sqlite3

import pytest

from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.native import NativeWorkflow


class Records(NativeWorkflow):
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE artifacts(id TEXT PRIMARY KEY,kind TEXT,status TEXT,input_refs_json TEXT)")

    def _get(self, c, table, identity):
        return dict(c.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone())

    def add(self, identity, *refs, status="accepted", kind="research.note"):
        self.connection.execute("INSERT INTO artifacts VALUES(?,?,?,?)",
                                (identity, kind, status, json.dumps(list(refs))))


@pytest.fixture
def records():
    instance = Records()
    yield instance
    instance.connection.close()


def test_accepted_inputs_stay_available_regardless_of_the_library_tree(records):
    records.add("statement", kind="lean.statement")
    records.add("note", "statement")
    records.add("derived-note", "note")
    assert Hub._available_artifact(records, records.connection, "derived-note")


@pytest.mark.parametrize("status", ["withdrawn", "rejected", "staged"])
def test_an_unaccepted_transitive_input_makes_dependents_unavailable(records, status):
    records.add("statement", kind="lean.statement", status=status)
    records.add("note", "statement")
    records.add("derived-note", "note")
    assert not Hub._available_artifact(records, records.connection, "derived-note")
    assert records._get(records.connection, "artifacts", "derived-note")["status"] == "accepted"
