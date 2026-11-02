"""Only sanitized operational markers rotate; conversations remain elsewhere."""
import gzip
import json

from formal_geyser.coordination.runtime.telemetry import ActivityLog


async def test_rotation_keeps_exact_markers_and_is_shared_across_runners(tmp_path):
    first = ActivityLog(tmp_path, max_bytes=1, backups=2)
    second = ActivityLog(tmp_path, max_bytes=1, backups=2)
    assert first._lock is second._lock
    for number in range(4):
        await (first if number % 2 else second).emit("turn_start", worker_id=f"worker-{number}")
    assert json.loads(first.path.read_text())["worker_id"] == "worker-3"
    retained = sorted(tmp_path.glob("*.gz"))
    assert len(retained) == 2
    assert json.loads(gzip.decompress(retained[0].read_bytes()))["worker_id"] == "worker-2"
    assert json.loads(gzip.decompress(retained[1].read_bytes()))["worker_id"] == "worker-1"
