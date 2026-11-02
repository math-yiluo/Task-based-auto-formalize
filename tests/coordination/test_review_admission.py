"""A negative batch hint throttles work without granting cached authority."""
from tests.coordination.test_integrator_batches import batch_case as batch_case
from tests.coordination.test_integrator_batches import choose, ready


async def test_blocked_candidates_do_not_repeat_exact_checks_on_each_poll(batch_case, monkeypatch):
    hub, run, now = batch_case
    for i in range(5):
        await ready(hub, run, f"Contribution {i}")
    original = hub._batch_eligible
    calls = []
    blocked = [True]
    def eligible(c, **kwargs):
        calls.append(1)
        return [] if blocked[0] else original(c, **kwargs)
    monkeypatch.setattr(hub, "_batch_eligible", eligible)
    for _ in range(20):
        assert await choose(hub, run) is None
    assert len(calls) == 1
    # A filesystem repair without a ledger event is discovered on the bounded
    # refresh; eligibility is then actually recomputed before admission.
    now[0] += 30_000
    blocked[0] = False
    assert await choose(hub, run) is not None
    assert len(calls) == 2


async def test_material_event_invalidates_negative_hint_immediately(batch_case, monkeypatch):
    hub, run, _ = batch_case
    for i in range(5):
        await ready(hub, run, f"Contribution {i}")
    original = hub._batch_eligible
    monkeypatch.setattr(hub, "_batch_eligible", lambda c, **kwargs: [])
    assert await choose(hub, run) is None
    monkeypatch.setattr(hub, "_batch_eligible", original)
    await hub.db.call(lambda c: hub._event(c, "validation.recovered", "fixture", {}), write=True)
    assert await choose(hub, run) is not None
