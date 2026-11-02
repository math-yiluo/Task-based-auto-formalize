"""Retain one epoch total, preserving exactly-once increments and regressions."""
from uuid import uuid4

from formal_geyser.coordination.models import Actor, Command, Role
from tests.coordination.test_archive_publication import archive_case as archive_case


async def test_many_cumulative_samples_keep_one_row_and_never_refund(archive_case):
    hub = archive_case.hub
    worker_id = archive_case.author["worker_id"]
    async def record(total, key=None):
        return await hub.execute(Command(name="usage.record", key=key or uuid4().hex,
            payload={"worker_id": worker_id, "sample": {"totalTokens": total}}), Actor(role=Role.RUNNER))
    for total in range(1, 31):
        await record(total)
    duplicate = uuid4().hex
    first = await record(31, duplicate)
    assert await record(31, duplicate) == first
    assert (await record(31))["charged_tokens"] == 0
    assert (await record(20))["uncertain"]
    assert (await hub.run(archive_case.run["id"]))["tokens_used"] == 31
    for table in ("usage_samples", "native_usage_samples"):
        assert await hub.db.read(lambda c: c.execute(f"SELECT count(*) FROM {table} WHERE worker_id=?", (worker_id,)).fetchone()[0]) == 1


async def test_duplicate_usage_does_not_rewrite_the_cumulative_rows(archive_case):
    hub, worker_id = archive_case.hub, archive_case.author["worker_id"]
    now = [hub.clock()]
    hub.clock = lambda: now[0]
    async def record():
        return await hub.execute(Command(name="usage.record", key="runtime:" + uuid4().hex,
            payload={"worker_id": worker_id, "sample": {"totalTokens": 10}}), Actor(role=Role.RUNNER))
    await record()
    before = await hub.db.read(lambda c: [dict(c.execute(f"SELECT * FROM {table} WHERE worker_id=?", (worker_id,)).fetchone())
                                        for table in ("usage_samples", "native_usage_samples")])
    now[0] += 1000
    assert (await record())["charged_tokens"] == 0
    after = await hub.db.read(lambda c: [dict(c.execute(f"SELECT * FROM {table} WHERE worker_id=?", (worker_id,)).fetchone())
                                       for table in ("usage_samples", "native_usage_samples")])
    assert after == before
