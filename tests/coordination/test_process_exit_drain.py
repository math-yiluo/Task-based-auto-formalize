"""Natural tree teardown must not be mistaken for surviving subprocess work."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from formal_geyser.coordination.runtime.process import OwnedProcessHost


@pytest.mark.parametrize("survives", [False, True])
async def test_natural_exit_waits_for_owned_tree_before_deciding_to_force(monkeypatch, survives):
    async def wait():
        return 0

    process = SimpleNamespace(stdin=SimpleNamespace(is_closing=lambda: True),
                              stdout=object(), stderr=object(), returncode=0, wait=wait)
    host = OwnedProcessHost(process)
    inspections, kills = [], []

    def active():
        inspections.append(1)
        # The first process can exit before its descendants have. A real survivor
        # remains until killed.
        return not kills and (survives or len(inspections) <= 2)

    monkeypatch.setattr(host, "_active_processes", active)
    monkeypatch.setattr(host, "_kill_tree", lambda **kwargs: kills.append(kwargs))
    result = await host.stop(grace=0.08)
    assert result.tree_quiet and result.returncode == 0
    assert result.forced is survives
    assert bool(kills) is survives
    assert await host.stop(grace=0.01) == result
