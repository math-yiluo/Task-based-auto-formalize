"""Retention observes only idle namespaces and tolerates disappearing files."""
import asyncio
import json
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from formal_geyser import lake_cache
from formal_geyser.lake_build import _namespace_lock


def write(path, text="cached"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def test_build_wait_for_maintenance_is_async_and_cancellable(tmp_path):
    namespace = "d" * 20
    lock_path = tmp_path / "locks" / (namespace + ".lock")
    entered = asyncio.Event()

    async def build_lock():
        async with _namespace_lock(tmp_path, namespace):
            entered.set()

    with lake_cache.lock_file(lock_path) as held:
        assert held
        waiting = asyncio.create_task(build_lock())
        try:
            await asyncio.sleep(0.02)  # Other service work can run while waiting.
            assert not entered.is_set()
        finally:
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
    await asyncio.wait_for(build_lock(), 1)
    assert entered.is_set()
    with lake_cache.lock_file(lock_path) as held:
        assert held  # Neither cancelled waiters nor completed builds leak a lock.


def test_size_skips_a_file_disappearing_between_checks(tmp_path, monkeypatch):
    lost, kept = tmp_path / "lost", tmp_path / "kept"
    write(lost)
    write(kept)
    original = Path.stat
    calls = 0

    def racing(path, *args, **kwargs):
        nonlocal calls
        if path == lost:
            calls += 1
            if calls == 2:
                lost.unlink()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", racing)
    assert lake_cache._size(tmp_path) == len("cached")
    assert not lost.exists()


@pytest.mark.parametrize("protection", ["caller", "namespace", "legacy_slot"])
def test_collector_never_sizes_active_outputs_or_artifacts(tmp_path, monkeypatch, protection):
    active_key, idle_key = "a" * 64, "b" * 64
    active = tmp_path / "workspaces" / active_key[:20]
    idle = tmp_path / "workspaces" / idle_key[:20]
    for namespace, key in ((idle, idle_key), (active, active_key)):
        write(namespace / "0" / "source.lean")
        write(namespace / "0" / "last-used")
        write(tmp_path / "cache" / key / "artifact")
    os.utime(idle, (1, 1))
    os.utime(active, (2, 2))
    original = lake_cache._size
    visited = []

    def idle_size(path):
        visited.append(path)
        assert path not in (tmp_path / "workspaces", tmp_path / "cache")
        assert not path.is_relative_to(active)
        assert not path.is_relative_to(tmp_path / "cache" / active_key)
        return original(path)

    monkeypatch.setattr(lake_cache, "_size", idle_size)
    with ExitStack() as locks:
        if protection != "caller":
            lock = (active / "0" / "build.lock" if protection == "legacy_slot" else
                    tmp_path / "locks" / (active.name + ".lock"))
            assert locks.enter_context(lake_cache.lock_file(lock, shared=protection == "namespace"))
        report = lake_cache.collect(tmp_path, now=3, max_bytes=0, force=True,
                                    active_namespace=active.name if protection == "caller" else None)
    assert report["active_namespaces_skipped"] == 1 and not report["errors"]
    assert report["size_scope"] == "observed_idle_namespaces"
    assert report["removed_bytes"] > 0
    assert (active / "0" / "source.lean").exists()
    assert (tmp_path / "cache" / active_key / "artifact").exists()
    assert not (idle / "0" / "source.lean").exists()
    assert not (tmp_path / "cache" / idle_key).exists()
    assert (idle / "0" / "build.lock").exists()
    assert json.loads((tmp_path / "retention.json").read_text()) == report
    previous_visits = list(visited)
    assert lake_cache.collect(tmp_path, now=4) == report
    assert visited == previous_visits  # The successful pass was throttled.


def test_namespace_claimed_after_sizing_is_not_removed(tmp_path, monkeypatch):
    key = "c" * 64
    namespace = tmp_path / "workspaces" / key[:20]
    output = namespace / "0" / "source.lean"
    write(output)
    write(tmp_path / "cache" / key / "artifact")
    original = lake_cache._idle_slots
    visits = 0
    with ExitStack() as running:
        @contextmanager
        def claimed_after_sample(root, path):
            nonlocal visits
            visits += 1
            with original(root, path) as slots:
                yield slots
            if visits == 1:
                assert running.enter_context(lake_cache.lock_file(
                    root / "locks" / (path.name + ".lock"), shared=True))

        monkeypatch.setattr(lake_cache, "_idle_slots", claimed_after_sample)
        report = lake_cache.collect(tmp_path, now=output.stat().st_mtime + 2, idle_seconds=0, force=True)
    assert visits == 2 and report["active_namespaces_skipped"] == 1
    assert output.exists() and not report["removed_bytes"]
