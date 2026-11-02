"""Repeated failures delay only evidenced recovery, without changing worker intent."""
from formal_geyser.coordination.models import Actor, Command, Role

from .test_continuous_workers import runtime as runtime


async def test_repeat_failures_back_off_and_notify_once_without_pausing(runtime, monkeypatch):
    hub, run = runtime
    now = [hub.clock()]
    monkeypatch.setattr(hub, "clock", lambda: now[0])
    identity = None
    for index, seconds in enumerate((60, 120, 240, 480, 960, 1920, 3600, 3600)):
        attempt = await hub.next_assignment(run["id"])
        assert attempt is not None
        identity = identity or attempt["worker_id"]
        assert attempt["worker_id"] == identity
        await hub.execute(Command(name="runtime.preflight_hold", key=f"failure-{index}", payload={
            "worker_id": identity, "attempt_id": attempt["id"], "reason": "RpcError: frame too large"}), Actor(role=Role.RUNNER))
        now[0] += seconds * 1000 - 1
        assert await hub.recover_quiet_runtime_failures(run["id"]) == []
        now[0] += 1
        assert await hub.recover_quiet_runtime_failures(run["id"]) == [identity]
        assert (await hub.run(run["id"]))["state"] == "running"
        assert (await hub.worker(identity))["work_intention"] == "working"
    notices = await hub.db.call(lambda c: c.execute(
        "SELECT count(*) FROM events WHERE kind='runtime.repeated_failure' AND object_id=?", (identity,)).fetchone()[0])
    assert notices == 1
    attempt = await hub.next_assignment(run["id"])
    await hub.execute(Command(name="runtime.preflight_hold", key="changed-error", payload={
        "worker_id": identity, "attempt_id": attempt["id"], "reason": "Different network fault"}), Actor(role=Role.RUNNER))
    now[0] += 60000
    assert await hub.recover_quiet_runtime_failures(run["id"]) == [identity]


def test_frame_envelope_change_preserves_identity_but_not_account_model_or_runtime_change():
    from formal_geyser.coordination.config import codex_profiles_equal
    profile = {"home": "profile-a", "model": "model-a", "version": "0.160.0", "max_frame_bytes": 8388608}
    assert codex_profiles_equal(profile, {**profile, "max_frame_bytes": 67108864})
    for field in ("home", "model", "version"):
        assert not codex_profiles_equal(profile, {**profile, field: "changed"})
