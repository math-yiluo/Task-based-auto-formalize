"""An idle run is looked at less often until a command wakes it, without models."""
from __future__ import annotations

from time import monotonic

from formal_geyser.coordination.models import Command
from formal_geyser.coordination.run_driver import IDLE_MAX_SECONDS, RunDriver


async def test_an_idle_run_is_looked_at_less_often_until_work_or_a_command_wakes_it():
    reads, steps = [], [0]

    class Hub:
        async def run(self, run_id, **kwargs):
            reads.append(run_id)
            return {"state": "running"}

    class Runner:
        async def run_until_idle(self, run_id, **_):
            return {"run_id": run_id, "state": "idle", "steps": steps[0]}

    driver = RunDriver(Hub(), make_runner=lambda hub: Runner(), resume_runs=False)
    driver.armed.add("run")

    async def one_pass():
        driver.queue("run")  # As the timer does once the run is due.
        await driver.passes["run"]
        return round(driver.idle_until["run"] - monotonic()) if "run" in driver.idle_until else None
    assert [await one_pass() for _ in range(6)] == [1, 2, 4, 8, IDLE_MAX_SECONDS, IDLE_MAX_SECONDS]
    assert len(reads) == 6  # A pass reads the run's controls once; the timer does not read them first.
    driver.committed(Command(name="task.publish", key="publish", payload={}))
    await driver.passes["run"]
    assert driver.idle_passes["run"] == 1  # Woken: looked at again after a second.
    steps[0] = 1
    assert await one_pass() is None and "run" not in driver.idle_passes  # Work started: every second.


async def test_a_paused_run_backs_off_and_its_resume_wakes_it_at_once():
    state = ["paused"]

    class Hub:
        async def run(self, run_id, **kwargs):
            return {"state": state[0]}

    driven = []

    class Runner:
        async def run_until_idle(self, run_id, **_):
            driven.append(run_id)
            return {"run_id": run_id, "state": "idle", "steps": 1}

    driver = RunDriver(Hub(), make_runner=lambda hub: Runner(), resume_runs=False)
    driver.armed.add("run")
    for _ in range(3):
        driver.queue("run")
        await driver.passes["run"]
    assert driver.idle_passes["run"] == 3 and driver.results["run"]["state"] == "not_running" and not driven
    state[0] = "running"
    driver.committed(Command(name="run.resume", key="resume", payload={"run_id": "run"}))
    await driver.passes["run"]
    assert driven == ["run"] and "run" not in driver.idle_until


async def test_an_operator_claim_goes_to_its_runs_pass():
    class Hub:
        async def run(self, run_id, **kwargs):
            return {"state": "running"}

    given = []

    class Runner:
        async def run_until_idle(self, run_id, claims):
            given.append((run_id, list(claims)))
            claims.clear()  # Launched.
            return {"run_id": run_id, "state": "idle", "steps": 1}

    driver = RunDriver(Hub(), make_runner=lambda hub: Runner(), resume_runs=False)
    driver.armed.add("run")
    claim = {"id": "attempt", "run_id": "run", "worker_id": "worker", "task_id": "task"}
    driver.committed(Command(name="task.join", key="join", payload={}), claim)
    await driver.passes["run"]
    assert given == [("run", [claim])]
    driver.committed(Command(name="task.publish", key="publish", payload={}), {"id": "task", "run_id": "run"})
    await driver.passes["run"]
    assert given[-1] == ("run", [])  # Only a claim command's result is a claim.
