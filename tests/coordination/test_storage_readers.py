"""Reader isolation under a blocked writer, including shutdown and failed reads."""
import asyncio
import sqlite3
import threading

import pytest

from formal_geyser.coordination.storage import Database


async def test_sql_readers_do_not_wait_for_writer_and_keep_consistent_snapshot(tmp_path):
    db = Database(tmp_path / "state.sqlite3", "CREATE TABLE state(n INTEGER);")
    await db.write(lambda c: c.execute("INSERT INTO state VALUES(1)"))
    entered, release = threading.Event(), threading.Event()
    def update(c):
        c.execute("UPDATE state SET n=2")
        entered.set()
        assert release.wait(10)
    pending = asyncio.create_task(db.write(update))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        values = await asyncio.wait_for(asyncio.gather(*(
            db.read(lambda c: c.execute("SELECT n FROM state").fetchone()[0]) for _ in range(8))), 2)
        assert values == [1] * 8
    finally:
        release.set()
        await pending
    assert await db.read(lambda c: c.execute("SELECT n FROM state").fetchone()[0]) == 2
    await db.close()


async def test_readers_refuse_writes_and_release_failed_snapshot(tmp_path):
    db = Database(tmp_path / "state.sqlite3", "CREATE TABLE state(n INTEGER);")
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await db.read(lambda c: c.execute("INSERT INTO state VALUES(1)"))
        await db.write(lambda c: c.execute("INSERT INTO state VALUES(2)"))
        for _ in range(3):
            assert await db.read(lambda c: c.execute("SELECT n FROM state").fetchone()[0]) == 2
        assert (await db.maintain())["checkpoint"][0] == 0
    finally:
        await db.close()
    with pytest.raises(RuntimeError, match="closed"):
        await db.read(lambda c: None)


async def test_reader_initialization_runs_recovery_once_on_writer(tmp_path):
    opened = []
    db = Database(tmp_path / "state.sqlite3", "CREATE TABLE state(n INTEGER);",
                  on_open=lambda c: opened.append(threading.current_thread().name))
    try:
        assert await db.read(lambda c: c.execute("PRAGMA query_only").fetchone()[0]) == 1
        assert await db.read(lambda c: c.execute("PRAGMA auto_vacuum").fetchone()[0]) == 2
        assert len(opened) == 1 and opened[0].startswith("taskhub-db")
    finally:
        await db.close()


async def test_idle_maintenance_skips_a_busy_writer(tmp_path):
    db = Database(tmp_path / "state.sqlite3", "CREATE TABLE state(n);")
    await db.write(lambda c: c.execute("INSERT INTO state VALUES(1)"))
    entered, release = threading.Event(), threading.Event()
    def slow(c):
        entered.set()
        assert release.wait(10)
    pending = asyncio.create_task(db.write(slow))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        db._maintenance_at = 0
        assert await asyncio.wait_for(db.maintain_if_idle(), 1) is None
    finally:
        release.set()
        await pending
    assert await db.maintain_if_idle() is not None
    assert await db.maintain_if_idle() is None
    await db.close()
