from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from formal_geyser.coordination.supervisor import ServiceSupervisor


@pytest.mark.parametrize("state,armed", [
    ("running", False), ("running", True), ("paused", False), ("stopped", False), ("complete", False),
])
def test_supervisor_only_observes_a_reachable_service(tmp_path, monkeypatch, state, armed):
    supervisor = ServiceSupervisor(tmp_path, "run")
    requests = []

    def request(method, path):
        requests.append((method, path))
        return {"state": state, "armed": armed}

    monkeypatch.setattr(supervisor, "request", request)
    assert supervisor.tick() == {"state": "observing", "run_state": state, "armed": armed}
    assert all(method == "GET" for method, _ in requests)


@pytest.mark.parametrize("port_open,starting", [(True, False), (False, True)])
def test_slow_or_starting_service_is_not_replaced(tmp_path, monkeypatch, port_open, starting):
    supervisor = ServiceSupervisor(tmp_path, "run")
    monkeypatch.setattr(supervisor, "request", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(supervisor, "port_open", lambda: port_open)
    if starting:
        supervisor.process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(supervisor, "start_service", lambda: pytest.fail("Do not replace a live service"))
    assert supervisor.tick()["state"] == "waiting_for_service"


def test_missing_service_is_restarted_once_with_backoff(tmp_path, monkeypatch):
    supervisor = ServiceSupervisor(tmp_path, "run")
    monkeypatch.setattr(supervisor, "request", lambda *_: (_ for _ in ()).throw(ConnectionError()))
    monkeypatch.setattr(supervisor, "port_open", lambda: False)
    starts = []
    monkeypatch.setattr(supervisor, "start_service", lambda: starts.append(1) or 123)
    assert supervisor.tick() == {"state": "service_started", "pid": 123}
    assert supervisor.tick()["state"] == "retry_pending"
    assert starts == [1]


@pytest.mark.parametrize("temporary", [False, True])
def test_log_failure_does_not_stop_supervision(tmp_path, monkeypatch, temporary):
    import formal_geyser.coordination.supervisor as module

    supervisor = ServiceSupervisor(tmp_path, "run")
    ticks, log_attempts = [], []
    original_open = Path.open

    def open_log(path, *args, **kwargs):
        if path.name == "supervisor-events.jsonl":
            log_attempts.append(1)
            if len(log_attempts) == 1 or not temporary:
                raise PermissionError("Synthetic temporary log contention")
        return original_open(path, *args, **kwargs)

    def sleep(_seconds):
        if len(ticks) == 3:
            (supervisor.state_dir / "supervisor.stop").touch()

    monkeypatch.setattr(Path, "open", open_log)
    monkeypatch.setattr(supervisor, "tick", lambda: ticks.append(1) or {"state": "observing"})
    monkeypatch.setattr(module.time, "sleep", sleep)
    supervisor.run()
    assert len(ticks) == 3
    assert len(log_attempts) == (2 if temporary else 3)
