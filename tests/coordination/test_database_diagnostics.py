from __future__ import annotations

import json
import logging
import re
import sqlite3
from uuid import uuid4

import pytest

from formal_geyser.coordination import storage
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import Actor, Command, ProjectConfig, Role


@pytest.mark.parametrize("timings,expected", [((0, 11, 12), True), ((0, 0, 13), True), ((0, 1, 2), False),
                                              ((0, 0, 2.5), True)])
async def test_database_diagnostics_distinguish_queue_and_execution_delay(tmp_path, monkeypatch, caplog, timings, expected):
    db = storage.Database(tmp_path / "state.sqlite3", "CREATE TABLE sample(n INTEGER);")
    ticks = iter(timings)
    monkeypatch.setattr(storage, "monotonic", lambda: next(ticks))
    try:
        result = await db.call(lambda c: c.execute("SELECT 1").fetchone()[0])
        assert result == 1
        assert ("Slow coordination database call" in caplog.text) is expected
        if expected:
            assert f"queue_seconds={timings[1] - timings[0]:.3f}" in caplog.text
            assert f"execute_seconds={timings[2] - timings[1]:.3f}" in caplog.text
            # A UTC timestamp and the queue depth line the report up with events.
            assert re.search(r"at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z ", caplog.text)
            assert "queue_depth=1 " in caplog.text
    finally:
        await db.close()


async def test_slow_failed_transaction_is_reported_without_payload_or_lost_rollback(tmp_path, monkeypatch, caplog):
    db = storage.Database(tmp_path / "state.sqlite3", "CREATE TABLE sample(n INTEGER UNIQUE);")
    ticks = iter((0, 0, 13, 13, 13, 13))
    monkeypatch.setattr(storage, "monotonic", lambda: next(ticks))

    def invalid(c):
        c.execute("INSERT INTO sample VALUES(1)")
        raise sqlite3.IntegrityError("private command details must not be logged")

    try:
        with pytest.raises(sqlite3.IntegrityError):
            await db.call(invalid, write=True)
        assert await db.call(lambda c: c.execute("SELECT count(*) FROM sample").fetchone()[0]) == 0
        assert "execute_seconds=13.000" in caplog.text
        assert "private command details" not in caplog.text
    finally:
        await db.close()


async def test_labels_name_operations_and_windows_summarize_every_call(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="formal_geyser.coordination.storage")
    db = storage.Database(tmp_path / "state.sqlite3", "CREATE TABLE sample(n INTEGER);")
    try:
        await db.call(lambda c: c.execute("SELECT 1").fetchone(), label="query:sample")
        await db.write(lambda c: c.execute("INSERT INTO sample VALUES(1)"), label="command:sample.insert")
        current = db.diagnostics()["current"]
        assert db.diagnostics()["pending"] == 0
        assert {item["label"] for item in current["top"]} == {"query:sample", "command:sample.insert"}
        assert current["calls"] == 2 and db.diagnostics()["previous"] is None
        db.statistics.window = 0  # The next call closes the window.
        await db.call(lambda c: None, label="query:sample")
        summary = db.diagnostics()["previous"]
        assert summary["calls"] == 3 and db.diagnostics()["current"] is None
        sample = next(item for item in summary["top"] if item["label"] == "query:sample")
        assert sample["calls"] == 2
        line = next(record.getMessage() for record in caplog.records
                    if record.getMessage().startswith("Coordination database summary: "))
        assert json.loads(line.split(": ", 1)[1])["calls"] == 3
        # Unlabelled calls keep their callback's name.
        db.statistics.window = 300
        await db.call(lambda c: None)
        assert any("<lambda>" in item["label"] for item in db.diagnostics()["current"]["top"])
    finally:
        await db.close()


async def test_commands_are_labelled_by_name(tmp_path):
    initialize(tmp_path, ProjectConfig())
    hub = Hub(tmp_path)
    try:
        await hub.execute(Command(name="task.publish", key=uuid4().hex, payload={
            "contract": {"title": "Label", "objective": "Check labels."}}), Actor(role=Role.OPERATOR))
        labels = {item["label"] for item in hub.db.diagnostics()["current"]["top"]}
        assert "command:task.publish" in labels
        assert not any("Hub.execute" in label for label in labels)
    finally:
        await hub.close()
