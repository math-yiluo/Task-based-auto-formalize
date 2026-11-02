"""The Lean service's scheduling policy, on synthetic jobs and machines."""
from __future__ import annotations

from formal_geyser.lean_service.scheduler import GIB, Job, Machine, Policy, plan, rank

NOW = 10_000.0


def machine(threads=16, memory=24 * GIB, commit=100 * GIB, **kwargs):
    return Machine(threads=threads, memory=memory, commit=commit, **kwargs)


def job(id, lane="official", owner="w1", **kwargs):
    kwargs.setdefault("submitted_at", NOW - 1)
    kwargs.setdefault("memory", GIB // 2)
    kwargs.setdefault("commit", GIB)
    return Job(id=id, lane=lane, owner=owner, **kwargs)


def running(id, lane="official", owner="w1", threads=1, **kwargs):
    kwargs.setdefault("started_at", NOW - 10)
    return job(id, lane, owner, state="running", assigned=threads, threads=threads, **kwargs)


def starts(actions):
    return [(action.job_id, action.threads) for action in actions if action.kind == "start"]


def test_classes_are_strict_and_blocking_validations_go_first():
    jobs = [job("plain"), job("blocking", blocking=True, submitted_at=NOW - 0.5),
            job("check", lane="interactive", submitted_at=NOW)]
    strict = Policy(per_owner=9, validation_slots=0)
    assert [action.job_id for action in plan(jobs, machine(threads=2), NOW, strict)] == ["check", "blocking"]
    order = sorted(jobs, key=lambda item: rank(item, NOW))
    assert [item.id for item in order] == ["check", "blocking", "plain"]


def test_blocking_validations_keep_a_few_places_under_a_steady_interactive_load():
    checks = [job(f"check{n}", lane="interactive", owner=f"w{n}", submitted_at=NOW - 10 + n) for n in range(8)]
    validations = [job(f"review{n}", blocking=True, owner=f"a{n}", submitted_at=NOW - 30 + n) for n in range(3)]
    plain = job("plain", owner="b")
    started = [job_id for job_id, _ in starts(plan([*checks, *validations, plain], machine(threads=6), NOW,
                                                   Policy(per_owner=9, validation_slots=2)))]
    # Two validations a review waits for, then interactive checks; the third validation and the
    # one nobody waits for yet stay queued.
    assert started[:2] == ["review0", "review1"] and started[2:] == ["check0", "check1", "check2", "check3"]
    # A validation already running uses one of the places.
    busy = [running("review0", owner="a0"), *checks, *validations[1:], plain]
    assert [job_id for job_id, _ in starts(plan(busy, machine(threads=6), NOW, Policy(per_owner=9)))][:1] == ["review1"]


def test_waiting_official_work_ages_into_blocking_and_arrival_order_holds():
    old = job("old", submitted_at=NOW - 3600)
    blocking = job("blocking", blocking=True, submitted_at=NOW - 60)
    assert [item.id for item in sorted([blocking, old], key=lambda item: rank(item, NOW))] == ["old", "blocking"]
    young = job("young", submitted_at=NOW - 60)
    assert [item.id for item in sorted([young, blocking], key=lambda item: rank(item, NOW))] == ["blocking", "young"]


def test_an_integrator_job_goes_first_but_running_work_is_never_stopped_for_it():
    batch = job("batch", lane="integrator", owner="integrator", shape="build", threads=16, min_threads=4, processes=16)
    work = [running("build", shape="build", threads=6, processes=6), running("single", lane="interactive")]
    # Nine threads are free: it starts there, and nothing running is suspended or started over.
    actions = plan([*work, batch, job("waiting", lane="interactive")], machine(), NOW)
    assert [(action.kind, action.job_id, action.threads) for action in actions] == [("start", "batch", 9)]
    # One that needs the whole machine waits for the running build to end, still stopping
    # nothing; meanwhile only work that ends before then starts.
    whole = job("whole", lane="integrator", owner="integrator", shape="build", threads=16, min_threads=16,
                processes=16)
    busy = [running("long", threads=13, seconds=600), whole, job("short", lane="interactive", seconds=30),
            job("later", lane="interactive", owner="w2", seconds=900)]
    assert starts(plan(busy, machine(), NOW)) == [("short", 1)]


def test_the_integrator_gets_as_many_threads_as_memory_permits():
    batch = job("batch", lane="integrator", owner="integrator", shape="build", threads=16, processes=16,
                memory=2 * GIB)
    [start] = plan([batch], machine(memory=14 * GIB), NOW)  # 10 GiB after the 4 GiB reserve.
    assert start.threads == 5
    [start] = plan([batch], machine(memory=3 * GIB), NOW)  # Alone it runs even when nothing fits.
    assert start.threads == 1


def test_validation_places_never_go_ahead_of_an_integrator_job():
    batch = job("batch", lane="integrator", owner="integrator", shape="build", threads=4, min_threads=2, processes=4)
    review = job("review", blocking=True, owner="a")
    # Two threads are free: the Integrator's job takes them, not the validation a review waits for.
    assert starts(plan([running("busy", lane="interactive", threads=2), batch, review], machine(threads=4), NOW)) == [
        ("batch", 2)]


def test_other_work_shares_the_machine_with_a_running_integrator_job():
    batch = running("batch", lane="integrator", owner="integrator", threads=8)
    waiting = [job(f"new{i}", owner=f"w{i}", lane="interactive") for i in range(3)]
    assert len(starts(plan([batch, *waiting], machine(), NOW))) == 3


def test_new_work_waits_while_suspended_work_cannot_resume():
    paused = job("paused", state="suspended", assigned=12, threads=12)
    busy = running("busy", lane="interactive", threads=8)
    assert plan([paused, busy, job("new")], machine(), NOW) == []


def test_a_suspended_job_that_does_not_fit_resumes_alone_once_nothing_runs():
    # 4 GiB still to grow, and 7 GiB available is 3 GiB after the reserve: it does not fit.
    paused = job("paused", state="suspended", assigned=1, memory=4 * GIB)
    [action] = plan([paused, job("new", owner="w2")], machine(memory=7 * GIB), NOW)
    assert (action.kind, action.job_id, action.reason) == ("resume", "paused", "runs_alone")
    # A higher class goes first, as it would have preempted the suspended job.
    check = job("check", lane="interactive", owner="w2")
    assert starts(plan([paused, check, job("new", owner="w3")], machine(memory=7 * GIB), NOW)) == [("check", 1)]
    check.state, check.assigned = "running", 1
    assert plan([paused, check, job("new", owner="w3")], machine(memory=7 * GIB), NOW) == []


def test_a_restarted_build_keeps_its_place_at_the_head_of_its_class():
    first = job("first", submitted_at=NOW - 100, blocking=True)
    again = job("again", submitted_at=NOW - 1, requeues=1)
    assert starts(plan([first, again], machine(threads=1), NOW)) == [("again", 1)]


def test_memory_pressure_suspends_the_newest_lowest_class_job_and_admits_nothing():
    jobs = [running("old", started_at=NOW - 100), running("new", started_at=NOW - 5),
            running("check", lane="interactive", started_at=NOW - 1), job("waiting", lane="interactive")]
    [action] = plan(jobs, machine(memory=GIB), NOW)
    assert (action.kind, action.job_id, action.reason) == ("suspend", "new", "memory_critical")
    assert plan(jobs, machine(memory=2 * GIB + GIB // 2), NOW) == []  # Below the low watermark.


def test_suspended_jobs_holding_the_memory_are_stopped_once_nothing_runs():
    def paused(id, lane="official", started=NOW - 50, since=NOW - 60):
        return job(id, lane, state="suspended", assigned=1, started_at=started, suspended_at=since,
                   suspended_for="memory_critical")
    jobs = [paused("old", started=NOW - 100), paused("new"), paused("check", "interactive", NOW - 10), job("waiting")]
    # Below the low watermark nothing that runs could give memory back: the newest of the lowest
    # class stops and is queued again, one per decision.
    [action] = plan(jobs, machine(memory=2 * GIB), NOW)
    assert (action.kind, action.job_id, action.reason) == ("requeue", "new", "memory_exhausted")
    # Not within its cooldown, nor while something still runs, nor above the low watermark.
    assert plan([paused("old", since=NOW - 5), job("waiting")], machine(memory=2 * GIB), NOW) == []
    assert plan([*jobs, running("busy")], machine(memory=2 * GIB), NOW) == []
    assert all(action.kind != "requeue" for action in plan(jobs, machine(memory=8 * GIB), NOW))


def test_a_job_suspended_for_memory_waits_for_its_cooldown():
    paused = job("paused", state="suspended", assigned=1, suspended_at=NOW - 5, suspended_for="memory_critical")
    assert plan([paused, job("new", owner="w2")], machine(), NOW) == []
    paused.suspended_at = NOW - 60
    assert [action.kind for action in plan([paused, job("new", owner="w2")], machine(), NOW)] == ["resume", "start"]


def test_owners_take_turns_within_a_class_with_a_cap_each():
    jobs = [job(f"a{i}", owner="a", lane="interactive", submitted_at=NOW - 10 + i) for i in range(4)]
    jobs.append(job("b0", owner="b", lane="interactive", submitted_at=NOW))
    assert [name for name, _ in starts(plan(jobs, machine(), NOW, Policy(per_owner=2)))] == ["a0", "b0", "a1"]
    assert [name for name, _ in starts(plan(jobs, machine(threads=2), NOW, Policy(per_owner=2)))] == ["a0", "b0"]


def test_a_job_resuming_in_the_same_plan_counts_for_its_owner_and_the_validation_places():
    paused = job("paused", owner="a", state="suspended", assigned=1)
    queued = [job(f"a{i}", owner="a", submitted_at=NOW - 10 + i) for i in range(2)]
    actions = plan([paused, *queued], machine(), NOW, Policy(per_owner=2))
    assert [(action.kind, action.job_id) for action in actions] == [("resume", "paused"), ("start", "a0")]
    # Nor do validations go ahead of interactive checks past their places with it.
    blocking = [job(f"v{i}", owner=f"v{i}", blocking=True) for i in range(2)]
    check = job("check", lane="interactive", owner="c")
    actions = plan([paused, *blocking, check], machine(threads=3), NOW, Policy(per_owner=9, validation_slots=2))
    assert [(action.kind, action.job_id) for action in actions] == [("resume", "paused"), ("start", "v0"),
                                                                   ("start", "check")]


def test_builds_get_many_threads_single_checks_few_and_memory_limits_parallel_processes():
    build = job("build", shape="build", threads=12, processes=12, memory=GIB)
    single = job("single", owner="w2", threads=8, memory=GIB)
    actions = plan([build, single], machine(memory=14 * GIB), NOW)  # 10 GiB to pack.
    assert starts(actions) == [("build", 10)]  # Memory for ten processes; the check then waits.
    assert starts(plan([single], machine(), NOW)) == [("single", 2)]


def test_running_jobs_keep_room_for_their_expected_growth():
    growing = running("growing", memory=6 * GIB, observed_memory=GIB)
    newcomer = job("newcomer", owner="w2", memory=6 * GIB)
    assert starts(plan([growing, newcomer], machine(memory=14 * GIB), NOW)) == []  # 10 GiB less 5 of growth < 6.
    growing.observed_memory = 6 * GIB
    assert starts(plan([growing, newcomer], machine(memory=14 * GIB), NOW)) == [("newcomer", 1)]


def test_committed_memory_is_packed_too():
    heavy = job("heavy", commit=40 * GIB)
    assert starts(plan([heavy], machine(commit=60 * GIB), NOW)) == [("heavy", 1)]
    assert starts(plan([running("other", owner="w2"), heavy], machine(commit=40 * GIB), NOW)) == []


def test_easy_backfill_starts_only_work_that_cannot_delay_the_head():
    # Twelve of sixteen threads are busy for another 100 s; the head needs all sixteen.
    busy = running("busy", lane="interactive", owner="w0", threads=12, seconds=110)
    head = job("head", shape="build", threads=16, min_threads=16, processes=16, submitted_at=NOW - 50)
    short = job("short", owner="w2", seconds=30, submitted_at=NOW - 40)
    long = job("long", owner="w3", seconds=500, submitted_at=NOW - 30)
    actions = plan([busy, head, short, long], machine(), NOW, Policy(per_owner=9))
    assert starts(actions) == [("short", 1)]
    # With threads to spare when the head starts, long work may use them.
    head.min_threads = head.threads = head.processes = 12
    assert starts(plan([busy, head, short, long], machine(), NOW, Policy(per_owner=9))) == [("short", 1), ("long", 1)]


def test_a_job_that_never_fits_runs_alone_and_meanwhile_only_shorter_work_starts():
    huge = job("huge", memory=40 * GIB)
    assert starts(plan([huge], machine(), NOW)) == [("huge", 1)]
    busy = running("busy", owner="w0", seconds=100)
    short, long = job("short", owner="w2", seconds=20), job("long", owner="w3", seconds=900)
    huge.submitted_at = NOW - 100
    assert starts(plan([busy, huge, short, long], machine(), NOW, Policy(per_owner=9))) == [("short", 1)]
