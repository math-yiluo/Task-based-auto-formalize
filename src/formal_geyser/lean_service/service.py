"""The Lean service's core: its jobs, the scheduling loop, execution and measurement.

Clients submit jobs and wait for them. A development check (interactive lane) belongs to
its client: when nobody waits for it any more it is abandoned. An official check (job
lane) is the shared, durable record of one frozen input: the same input under the same
environment is one job, its definitive result is reused, and it survives pauses, client
restarts and the service's own restarts. Every second the scheduler decides what runs;
the service carries that out and measures each job's processes, so that the next similar
job is sized from what this one really used.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..coordination.models import LeanServiceConfig
from ..coordination.storage import digest
from ..process import ProcessRunner, process_environment
from . import machine as machine_module
from .executor import JobSpec, content_key, memory_limited, run_check, stopped_reason
from .osjobs import JobGroup
from .sandbox import Unsandboxed
from .scheduler import CLASSES, GIB, Job, Machine, Policy, plan, rank
from .sessions import SessionPool
from .store import RESULT_DAYS, Store
from .warm import WarmSet

logger = logging.getLogger(__name__)
# Per Lean process, until jobs like it have been measured. In the Linux pilot a measured check's
# peak was 1.3 GiB private and 1.4 to 1.7 GiB committed, nearly whatever it checked: these are
# its 90th percentiles, and the least a Lean process is assumed to take is below them.
DEFAULT_MEMORY, DEFAULT_COMMIT = 27 * GIB // 20, 33 * GIB // 20  # 1.35 and 1.65 GiB
MIN_MEMORY, MIN_COMMIT = GIB, 5 * GIB // 4
DEFAULT_SECONDS = {"interactive": 60.0, "official": 180.0, "integrator": 900.0}
SAFETY = 1.15  # Estimates are the last observed peak with this margin.
CLIENT_SECONDS = 60.0  # A development check nobody has asked about for this long is abandoned.
SAMPLE_SECONDS = 10.0
TICK_SECONDS = 1.0
# A reading of free memory serves this long while the next ones fail; past it free memory is
# unknown and nothing starts or resumes: taken as unlimited, it would start every queued check.
MEASUREMENT_SECONDS = 30.0
# A job's cgroup that could not be removed is tried again after this long, doubling to the most.
CLEANUP_SECONDS, CLEANUP_MAX_SECONDS = 5.0, 300.0
# bwrap, which the Codex sandbox runs, refuses a command of more than 9,000 arguments in all. On
# 2026-11-01 a validation in the Linux pilot passed one Lake target for each of 9,429 modules (the
# Library's 8,674 and a candidate's 755) and failed before Lean ran. A longer command reads its
# arguments from a file instead (argument_file_command).
ARGUMENT_FILE_LIMIT = 1000


def argument_file_command(argv, directory: Path) -> tuple[list[str], Path]:
    """`argv` as a short command whose shell, inside the sandbox, reads all its arguments but the
    executable from a file in `directory`: the returned path, for the caller to remove. The shell
    splits the file at whitespace, so no argument may contain any."""
    arguments = [str(item) for item in argv[1:]]
    if any(not item or any(character.isspace() for character in item) for item in arguments):
        raise ValueError("An argument file holds only nonempty arguments without whitespace")
    path = directory / f"arguments-{uuid4().hex}"
    path.write_text("\n".join(arguments) + "\n", encoding="utf-8")
    # `set -f`: the arguments are not file name patterns.
    return ["/bin/sh", "-c", 'set -f; exec "$0" $(cat "$1")', str(argv[0]), str(path)], path


def estimate_key(spec: JobSpec) -> str:
    """Jobs on the same modules; a job nothing like has run yet takes the defaults."""
    request = spec.request
    return digest({"kind": spec.kind, "modules": sorted(request.modules), "replay": request.kernel_replay,
                   "inspections": len(request.inspections), "official": request.snapshot is not None})


class ServiceJob:
    def __init__(self, row: dict[str, Any], spec: JobSpec, sched: Job):
        self.id = row["id"]
        self.row = row
        self.spec = spec
        self.sched = sched
        self.group: JobGroup | None = None
        self.task: asyncio.Task | None = None
        self.scratch: Path | None = None
        self.done = asyncio.Event()
        self.runnable = asyncio.Event()
        self.runnable.set()
        self.cancel_requested = False
        self.abandoned = False
        self.memory_limited: int | None = None  # What its largest process held when it was stopped for it.
        self.borrowed: list[JobGroup] = []  # A resident session's processes, while this job uses them.
        self.suspended_since: float | None = None
        self.run_paused_base = 0.0  # Suspended seconds before the current run started.
        self.peak_committed = 0
        self.cpu_seconds = 0.0

    def paused_total(self, now: float) -> float:
        return self.row["suspended_seconds"] + (now - self.suspended_since if self.suspended_since else 0.0)


@dataclass
class Lingering:
    """A job's cgroup that could not be removed. Something of the job may still run in it, so until
    it is removed (`_keep_removing`, without a time limit) its threads stay taken, its scratch stays,
    and the job, if it was queued again, does not start a second run beside the first."""
    group: JobGroup
    threads: int
    scratch: Path | None
    since: float
    task: asyncio.Task | None = None


class JobRunner:
    """Starts a job's Lean and Lake processes: in its cgroup, in the sandbox, never while it is
    suspended, and with its suspended time left out of any timeout."""

    def __init__(self, service: LeanService, job: ServiceJob):
        self.service = service
        self.job = job

    def paused_seconds(self) -> float:
        """How long the job has been suspended in all, which a time limit leaves out."""
        return self.job.paused_total(self.service.clock())

    def memory_stop(self) -> tuple[str, float, int] | None:
        """When the service stopped the job for its memory: its lane, the limit in GiB and what
        its largest process held (executor.memory_limited)."""
        if self.job.memory_limited is None:
            return None
        return self.job.sched.lane, self.service.config.process_memory_gib, self.job.memory_limited

    def borrow(self, group: JobGroup) -> None:
        """While the job uses a resident session, suspending the job suspends the session too."""
        self.job.borrowed.append(group)
        if self.job.sched.state == "suspended":
            group.suspend()

    def give_back(self, group: JobGroup) -> None:
        if group in self.job.borrowed:
            self.job.borrowed.remove(group)
            if group.suspended:
                group.resume()

    async def run(self, argv, *, cwd: Path, timeout: float | None = None, output_limit: int = 1048576,
                  env=None, stdin: bytes | None = None):
        job, service = self.job, self.service
        await job.runnable.wait()
        build = Path(argv[0]).name == "lake" and "build" in argv[1:2]
        if build:
            job.sched.shape = "build"
        assert job.scratch is not None
        # The sandbox lets a process write its working directory, so a process that needs no
        # particular one, such as a version probe, runs in the job's scratch directory.
        lake = Path(job.spec.request.blobs).parent / "lake"
        if not any(Path(cwd).resolve().is_relative_to(root.resolve()) for root in (lake / "workspaces", job.scratch)):
            cwd = job.scratch
        temporary = job.scratch / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        environment = dict(env if env is not None else process_environment())
        environment["TMPDIR"] = str(temporary)
        writable = [Path(cwd), job.scratch, *([Path(environment["LAKE_CACHE_DIR"])]
                                              if environment.get("LAKE_CACHE_DIR") else [])]
        for directory in writable:
            # Created here, so that the sandbox can grant them.
            directory.mkdir(parents=True, exist_ok=True)
        arguments = None
        if len(argv) > ARGUMENT_FILE_LIMIT:
            argv, arguments = argument_file_command(argv, temporary)
        try:
            command, environment = service.sandbox.wrap(argv, cwd=Path(cwd), env=environment, writable=writable)
            base = job.paused_total(service.clock())
            return await service.process_runner.run(
                command, cwd=cwd, timeout=timeout, output_limit=output_limit, env=environment, stdin=stdin,
                group=job.group, register=False, paused=lambda: job.paused_total(service.clock()) - base)
        finally:
            if arguments is not None:
                arguments.unlink(missing_ok=True)
            if build:
                # The rest of the check runs one Lean process at a time.
                job.sched.shape = "single"
                if job.sched.assigned > 1:
                    job.sched.assigned = 1
                    service.wake()


class LeanService:
    def __init__(self, root: Path, config: LeanServiceConfig | None = None, *, sandbox=None, runner=None,
                 measure=None, threads: int | None = None, clock=time.time, session_command=None):
        self.root = Path(root).resolve()
        self.config = config or LeanServiceConfig()
        self.state_dir = self.root / ".task-based" / "lean-service"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state_dir / "service.sqlite3")
        self.sandbox = sandbox or Unsandboxed()
        self.process_runner = runner or ProcessRunner()
        self.measure = measure or machine_module.memory
        self.threads = threads or self.config.threads or machine_module.logical_cores()
        self.clock = clock
        self.instance = uuid4().hex
        self.jobs: dict[str, ServiceJob] = {}
        self.finished: dict[str, ServiceJob] = {}  # Recent, for clients still waiting.
        self.lingering: dict[str, Lingering] = {}  # By job ID.
        self._measured: tuple[float, tuple[int, int]] | None = None  # The last reading of free memory.
        self._memory_unknown = False
        self.policy = Policy(per_owner=self.config.per_owner, aging_seconds=self.config.aging_minutes * 60,
                             validation_slots=self.config.validation_slots)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._cpu = machine_module.CpuMeter()
        self._last: dict[str, float | int | None] = {"memory_available": None, "commit_available": None,
                                                    "cpu_busy": None}
        self.warm: WarmSet | None = None  # Set by the server: the library pages it keeps resident.
        # Seconds the event loop ran the sampling timer late, in all: blocking work on the loop
        # delays every running job, and each job's result gives its share (`event_loop_lag`).
        self.loop_lag = 0.0
        self.sessions = SessionPool(sandbox=self.sandbox, limit=self.config.sessions,
                                    idle_seconds=self.config.session_idle_minutes * 60, command=session_command)
        self.warm_changed = asyncio.Event()

    # Lifecycle

    async def start(self) -> None:
        now = self.clock()
        for row in await asyncio.to_thread(self.store.live):
            spec = JobSpec.model_validate_json(row["spec_json"])
            restarted = row["state"] != "queued"
            row.update(state="queued", requeues=row["requeues"] + int(restarted),
                       last_polled_at=now if spec.lane == "interactive" else row["last_polled_at"])
            await asyncio.to_thread(self.store.update, row["id"], state="queued", requeues=row["requeues"],
                                    last_polled_at=row["last_polled_at"])
            estimate = await self._estimate(spec)
            job = ServiceJob(row, spec, self._sched(row["id"], spec, estimate, row["submitted_at"]))
            job.sched.requeues = row["requeues"]
            self.jobs[job.id] = job
        self._tasks = [asyncio.create_task(self._schedule_loop(), name="lean-service-schedule"),
                       asyncio.create_task(self._sample_loop(), name="lean-service-sample")]
        self.wake()

    async def close(self) -> None:
        """Stop: running jobs are queued again for the next start."""
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.sessions.close()
        running = [job.task for job in self.jobs.values() if job.task is not None and not job.task.done()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        removing = [item.task for item in self.lingering.values() if item.task is not None]
        for task in removing:
            task.cancel()
        await asyncio.gather(*removing, return_exceptions=True)
        for group in [*(job.group for job in self.jobs.values() if job.group is not None),
                      *(item.group for item in self.lingering.values())]:
            try:
                await asyncio.to_thread(group.close)
            except Exception:
                logger.exception("A Lean job's cgroup cannot be removed; the service's unit ends what is left in it")
        self.store.close()

    def wake(self) -> None:
        self._wake.set()

    # Client operations

    async def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        spec = JobSpec.model_validate(body)
        key = await asyncio.to_thread(content_key, spec)
        now = self.clock()
        if key is not None:
            for job in self.jobs.values():
                if job.row["content_key"] == key:
                    await self._upgrade(job, spec)
                    return {**self.view(job), "deduplicated": True}
            stored = await asyncio.to_thread(self.store.reusable, key, now - RESULT_DAYS * 86400)
            if stored is not None:
                return {**self._row_view(stored), "deduplicated": True}
        job_id = uuid4().hex
        row = {"id": job_id, "content_key": key, "estimate_key": estimate_key(spec), "lane": spec.lane,
               "kind": spec.kind, "owner": spec.owner, "label": spec.label, "blocking": int(spec.blocking),
               "spec_json": spec.model_dump_json(), "state": "queued", "submitted_at": now,
               "last_polled_at": now, "requeues": 0, "suspensions": 0, "suspended_seconds": 0.0, "threads": 0}
        await asyncio.to_thread(self.store.insert, row)
        estimate = await self._estimate(spec)
        job = ServiceJob(row, spec, self._sched(job_id, spec, estimate, now))
        self.jobs[job_id] = job
        self.wake()
        return {**self.view(job), "deduplicated": False}

    async def wait(self, job_id: str, timeout: float) -> dict[str, Any]:
        job = self.jobs.get(job_id) or self.finished.get(job_id)
        if job is None:
            row = await asyncio.to_thread(self.store.get, job_id)
            if row is None:
                raise KeyError(job_id)
            return self._row_view(row)
        job.row["last_polled_at"] = self.clock()
        if not job.done.is_set():
            try:
                await asyncio.wait_for(job.done.wait(), max(0.0, timeout))
            except TimeoutError:
                pass
        job.row["last_polled_at"] = self.clock()
        return self.view(job)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            return await self.wait(job_id, 0)
        if job.task is None:
            await self._finish(job, None, "cancelled")
        else:
            job.cancel_requested = True
            job.task.cancel()
            await job.done.wait()
        return self.view(job)

    def view(self, job: ServiceJob) -> dict[str, Any]:
        state = "done" if job.done.is_set() else job.sched.state
        view = {"id": job.id, "lane": job.sched.lane, "owner": job.row["owner"], "label": job.row["label"],
                "blocking": job.sched.blocking, "state": state, "outcome": job.row.get("outcome"),
                "submitted_at": job.row["submitted_at"], "started_at": job.sched.started_at,
                "threads": job.sched.assigned, "requeues": job.row["requeues"],
                "suspensions": job.row["suspensions"]}
        if state == "queued":
            queued = sorted((item.sched for item in self.jobs.values() if item.sched.state == "queued"),
                            key=lambda item: rank(item, self.clock(), self.policy))
            view["position"] = 1 + [item.id for item in queued].index(job.id)
        if state == "done":
            view["result"] = job.row.get("result")
        return view

    def _row_view(self, row: dict[str, Any]) -> dict[str, Any]:
        import json

        return {"id": row["id"], "lane": row["lane"], "owner": row["owner"], "label": row["label"],
                "blocking": bool(row["blocking"]), "state": row["state"], "outcome": row["outcome"],
                "submitted_at": row["submitted_at"], "started_at": row["started_at"], "threads": row["threads"],
                "requeues": row["requeues"], "suspensions": row["suspensions"],
                **({"result": json.loads(row["result_json"])} if row.get("result_json") else {})}

    def status(self) -> dict[str, Any]:
        now = self.clock()
        jobs = sorted(self.jobs.values(), key=lambda job: rank(job.sched, now, self.policy))
        counts = {state: sum(job.sched.state == state for job in jobs) for state in ("running", "queued", "suspended")}
        return {"instance": self.instance, "pid": os.getpid(), "sandbox": self.sandbox.name,
                "threads": self.threads, "threads_in_use": sum(job.sched.assigned for job in jobs
                                                               if job.sched.state == "running"),
                **self._last, "counts": counts, "sessions": self.sessions.status(),
                "jobs": [{"id": job.id, "lane": job.sched.lane, "owner": job.row["owner"], "label": job.row["label"],
                          "state": job.sched.state, "threads": job.sched.assigned, "blocking": job.sched.blocking,
                          "seconds": now - (job.sched.started_at if job.sched.state != "queued" and job.sched.started_at
                                            else job.row["submitted_at"]),
                          "private_memory": job.sched.observed_memory} for job in jobs],
                "cleanup": [{"id": job_id, "threads": item.threads, "seconds": now - item.since}
                            for job_id, item in self.lingering.items()],
                "warm": self.warm.status() if self.warm is not None else None}

    async def telemetry(self, hours: float) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.telemetry, self.clock() - hours * 3600)

    # Scheduling

    async def _estimate(self, spec: JobSpec) -> dict | None:
        # Jobs on the same modules only. The lane's last job is no estimate: in the Linux pilot a
        # cached check left 16 MB for every new interactive check, and 14 started at once.
        return await asyncio.to_thread(self.store.estimate, estimate_key(spec))

    def _sched(self, job_id: str, spec: JobSpec, estimate: dict | None, submitted_at: float) -> Job:
        modules = len(spec.request.modules)
        if estimate is not None:
            processes = max(1, estimate["processes"])
            # A check that Lake answered from its cache measures next to nothing; the next one on
            # the same modules may rebuild them.
            memory = max(MIN_MEMORY, int(estimate["memory"] * SAFETY))
            commit = max(MIN_COMMIT, int(estimate["committed"] * SAFETY))
            seconds = max(1.0, estimate["seconds"])
        else:
            processes, memory, commit = modules, DEFAULT_MEMORY, DEFAULT_COMMIT
            seconds = DEFAULT_SECONDS[spec.lane]
        processes = max(1, min(self.threads, processes))
        if spec.lane == "integrator":
            threads = self.policy.integrator_threads
        else:
            threads = max(self.policy.single_threads, processes) if modules > 1 else self.policy.single_threads
        threads = min(self.threads, threads)
        return Job(id=job_id, lane=spec.lane, owner=spec.owner, shape="build", threads=threads,
                   min_threads=max(1, min(threads, self.threads // 4)) if processes > 1 else 1,
                   memory=memory, commit=commit, processes=processes, seconds=seconds, blocking=spec.blocking,
                   submitted_at=submitted_at)

    async def _upgrade(self, job: ServiceJob, spec: JobSpec) -> None:
        """A second request for the same input may need it sooner."""
        changed = False
        if CLASSES[spec.lane] < CLASSES[job.sched.lane]:
            job.sched.lane = spec.lane
            changed = True
        if spec.blocking and not job.sched.blocking:
            job.sched.blocking = True
            changed = True
        if changed:
            job.row.update(lane=job.sched.lane, blocking=int(job.sched.blocking))
            await asyncio.to_thread(self.store.update, job.id, lane=job.sched.lane, blocking=int(job.sched.blocking))
            self.wake()

    def _machine(self) -> Machine | None:
        """What the machine has free now; None while free memory is unknown (MEASUREMENT_SECONDS).
        The threads a cgroup that could not be removed still holds are not free."""
        now = self.clock()
        measured = self.measure()
        if measured is not None:
            self._measured = (now, measured)
        elif self._measured is not None and now - self._measured[0] <= MEASUREMENT_SECONDS:
            measured = self._measured[1]
        if (measured is None) != self._memory_unknown:
            self._memory_unknown = measured is None
            logger.warning("Free memory cannot be measured: no Lean job starts or resumes until it can"
                           if measured is None else "Free memory is measured again")
        memory, commit = measured if measured is not None else (None, None)
        self._last.update(memory_available=memory, commit_available=commit)
        if memory is None or commit is None:
            return None
        config = self.config
        held = sum(item.threads for item in self.lingering.values())
        return Machine(threads=max(0, self.threads - held), memory=memory, commit=commit,
                       reserve=int(config.reserve_gib * GIB),
                       commit_reserve=int(config.commit_reserve_gib * GIB),
                       low_watermark=int(config.low_watermark_gib * GIB),
                       critical_watermark=int(config.critical_watermark_gib * GIB))

    async def _schedule_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.schedule()
            except Exception:
                logger.exception("Lean scheduling failed; it is retried")

    async def schedule(self) -> None:
        """One scheduling decision, carried out."""
        if self._closing:
            return
        machine = self._machine()
        if machine is None:
            return
        # A job queued again waits while its previous run's cgroup is lingering.
        actions = plan([job.sched for job in self.jobs.values() if job.id not in self.lingering], machine,
                       self.clock(), self.policy)
        for action in actions:
            job = self.jobs.get(action.job_id)
            if job is None:
                continue
            if action.kind == "start":
                await self._start(job, action.threads)
            elif action.kind == "suspend":
                await self._suspend(job, action.reason)
            elif action.kind == "resume":
                await self._resume(job)
            elif action.kind == "requeue":
                await self._requeue(job, action.reason)

    async def _start(self, job: ServiceJob, threads: int) -> None:
        now = self.clock()
        try:
            job.group = await asyncio.to_thread(JobGroup)
        except OSError as error:
            # No process of the job may run outside its cgroup: it ends, unavailable.
            logger.error("Lean job %s could not start: %s", job.id, error)
            await self._finish(job, {"error": {"type": "EnvironmentUnavailable", "message": str(error)},
                                     "definitive": False}, "failed")
            return
        job.scratch = self.state_dir / "scratch" / job.id
        job.scratch.mkdir(parents=True, exist_ok=True)
        job.sched.state, job.sched.assigned, job.sched.started_at = "running", threads, now
        job.sched.observed_memory = job.sched.observed_commit = 0
        job.run_paused_base = job.paused_total(now)
        job.runnable.set()
        job.row.update(state="running", started_at=now, threads=max(job.row["threads"], threads))
        fields = {"state": "running", "started_at": now, "threads": job.row["threads"]}
        if job.row.get("queue_seconds") is None:
            job.row["queue_seconds"] = fields["queue_seconds"] = now - job.row["submitted_at"]
        await asyncio.to_thread(self.store.update, job.id, **fields)
        job.task = asyncio.create_task(self._run(job, threads), name="lean-job-" + job.id)

    @staticmethod
    async def _signal(job: ServiceJob, action: str) -> bool:
        """Freeze or thaw a job's processes. False when the job ended before or meanwhile: a plan is
        carried out one action at a time, its release took the cgroup, and its state stays as it ended."""
        group = job.group
        if group is None:
            return False
        try:
            for each in (group, *job.borrowed):
                await asyncio.to_thread(getattr(each, action))
        except OSError:
            if job.group is group:
                raise
            return False
        return job.group is group

    async def _suspend(self, job: ServiceJob, reason: str) -> None:
        job.runnable.clear()
        if not await self._signal(job, "suspend"):
            return
        now = self.clock()
        job.sched.state, job.sched.suspended_at, job.sched.suspended_for = "suspended", now, reason
        job.suspended_since = now
        job.row.update(state="suspended", suspensions=job.row["suspensions"] + 1)
        await asyncio.to_thread(self.store.update, job.id, state="suspended", suspensions=job.row["suspensions"])

    async def _requeue(self, job: ServiceJob, reason: str) -> None:
        """Stop a suspended job, which holds memory it cannot use, and queue it again (_stopped)."""
        task = job.task
        if task is None or task.done():
            return
        logger.warning("Lean job %s is stopped and queued again: %s", job.id, reason)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _resume(self, job: ServiceJob) -> None:
        if not await self._signal(job, "resume"):
            return
        now = self.clock()
        job.row["suspended_seconds"] = job.paused_total(now)
        job.suspended_since = None
        job.sched.state, job.sched.suspended_at, job.sched.suspended_for = "running", None, None
        job.row["state"] = "running"
        job.runnable.set()
        await asyncio.to_thread(self.store.update, job.id, state="running",
                                suspended_seconds=job.row["suspended_seconds"])

    # Execution

    async def _run(self, job: ServiceJob, threads: int) -> None:
        # Development checks and validations end by time; the Integrator's job does not, since a
        # legitimate rebuild of much of the Library may take long and a timeout holds up its batch.
        lane = job.sched.lane
        minutes = {"interactive": self.config.check_minutes, "official": self.config.validation_minutes}.get(lane, 0)
        lag = self.loop_lag
        try:
            result = await run_check(job.spec.request, runner=JobRunner(self, job), threads=threads,
                                     scratch=job.scratch, sessions=self.sessions if self.config.sessions else None,
                                     owner=job.row["owner"], time_limit=minutes * 60 if minutes else None,
                                     time_limit_reason=stopped_reason(lane, minutes * 60) if minutes else None)
            if isinstance(result.get("timings"), dict):
                result["timings"]["event_loop_lag"] = round(self.loop_lag - lag, 3)
            outcome = "completed"
        except asyncio.CancelledError:
            await self._stopped(job)
            return
        except Exception as error:
            logger.exception("Lean job %s failed", job.id)
            result = {"error": {"type": "RuntimeError", "message": f"{type(error).__name__}: {error}"},
                      "definitive": False}
            outcome = "failed"
        await self._finish(job, result, outcome)

    async def _release(self, job: ServiceJob) -> bool:
        """End and measure whatever still runs in the job's cgroup, and remove it. False when it
        cannot be removed: it is then kept, lingering, with the job's threads and scratch."""
        if job.id in self.lingering:
            return False
        group, job.group = job.group, None
        if group is not None:
            try:
                usage = await asyncio.to_thread(group.usage)
                job.cpu_seconds += usage.cpu_seconds
                job.peak_committed = max(job.peak_committed, usage.peak_committed_memory)
                job.row["peak_private_memory"] = max(job.row.get("peak_private_memory") or 0, group.peak_private_memory)
            except Exception:
                logger.exception("Lean job %s: its last measurement failed", job.id)
            try:
                await asyncio.to_thread(group.close)
            except Exception:
                # The most it ran with: a build's threads are given back as it ends, cancelled too.
                threads = max(1, job.sched.assigned, job.row.get("threads") or 0)
                logger.exception("Lean job %s: its cgroup cannot be removed; it keeps %d threads until it is",
                                 job.id, threads)
                item = self.lingering[job.id] = Lingering(group, threads, job.scratch, self.clock())
                item.task = asyncio.create_task(self._keep_removing(job.id, item), name="lean-cleanup-" + job.id)
                return False
        if job.scratch is not None:
            await asyncio.to_thread(shutil.rmtree, job.scratch, True)
        return True

    async def _keep_removing(self, job_id: str, item: Lingering) -> None:
        delay = CLEANUP_SECONDS
        while True:
            await asyncio.sleep(delay)
            try:
                await asyncio.to_thread(item.group.close)
                break
            except Exception as error:
                delay = min(delay * 2, CLEANUP_MAX_SECONDS)
                logger.warning("Lean job %s: its cgroup still cannot be removed (%s); tried again in %.0f s",
                               job_id, error, delay)
        if item.scratch is not None:
            await asyncio.to_thread(shutil.rmtree, item.scratch, True)
        del self.lingering[job_id]
        logger.warning("Lean job %s: its cgroup was removed after %.0f s", job_id, self.clock() - item.since)
        self.wake()

    async def _stopped(self, job: ServiceJob) -> None:
        """A cancelled run: cancelled by its client, stopped for its memory (it ends unavailable), or
        stopped with the service and queued again (to start once its cgroup is gone)."""
        await self._release(job)
        if job.cancel_requested:
            await self._finish(job, None, "cancelled")
            return
        if job.memory_limited is not None:
            await self._finish(job, memory_limited(job.spec.request, job.sched.lane, self.config.process_memory_gib,
                                                   job.memory_limited), "completed")
            return
        job.task = None
        job.sched.state, job.sched.assigned, job.sched.started_at = "queued", 0, None
        job.sched.requeues += 1
        job.suspended_since = None
        job.runnable.set()
        job.row.update(state="queued", requeues=job.row["requeues"] + 1)
        await asyncio.to_thread(self.store.update, job.id, state="queued", requeues=job.row["requeues"],
                                suspended_seconds=job.row["suspended_seconds"])
        self.wake()

    async def _finish(self, job: ServiceJob, result: dict | None, outcome: str) -> None:
        try:
            await self._record(job, result, outcome)
        except Exception:
            logger.exception("Lean job %s: its end was not recorded", job.id)
        finally:
            # Waiting clients learn the end even when recording it failed.
            self.jobs.pop(job.id, None)
            self.finished[job.id] = job
            while len(self.finished) > 500:
                self.finished.pop(next(iter(self.finished)))
            job.done.set()
            self.wake()

    async def _record(self, job: ServiceJob, result: dict | None, outcome: str) -> None:
        import json

        # Every reader sees the result as stored: JSON.
        result = None if result is None else json.loads(json.dumps(result))
        job.row.update(outcome=outcome, result=result)
        now = self.clock()
        # The result stands when the cgroup lingers: the processes that gave it were confirmed
        # ended (ProcessRunner), and the cgroup keeps its threads until it is removed.
        await self._release(job)
        run_seconds =((now - job.sched.started_at - (job.paused_total(now) - job.run_paused_base))
                       if job.sched.started_at else None)
        definitive = bool(result and result.get("definitive")) and outcome == "completed"
        fields = {"state": "done", "outcome": outcome, "result": result, "definitive": int(definitive),
                  "finished_at": now, "run_seconds": run_seconds, "cpu_seconds": job.cpu_seconds,
                  "peak_private_memory": job.row.get("peak_private_memory"),
                  "peak_committed_memory": job.peak_committed or None,
                  "suspended_seconds": job.paused_total(now)}
        job.row.update(fields)
        await asyncio.to_thread(self.store.update, job.id, **fields)
        if self.warm is not None and result and result.get("library_imports"):
            roots = [Path(root["path"]) for root in job.spec.request.environment.get("library_roots", [])]
            if self.warm.note(roots, set(result["library_imports"])):
                self.warm_changed.set()
        # A run stopped for its memory sizes nothing: it did not end on its own.
        if (outcome == "completed" and run_seconds is not None and job.row.get("peak_private_memory")
                and job.memory_limited is None):
            compilation = (result or {}).get("compilation") or {}
            processes = max(1, min(job.row["threads"] or 1, len(compilation.get("built_modules") or []) or 1))
            await asyncio.to_thread(
                self.store.record_estimate, job.row["estimate_key"], memory=job.row["peak_private_memory"] // processes,
                committed=(job.peak_committed or job.row["peak_private_memory"]) // processes,
                processes=processes, seconds=run_seconds)

    # Measurement

    @staticmethod
    async def _sample_job(job: ServiceJob) -> None:
        """Measure a running job (what it uses now, and its peaks), or freeze a process that was
        starting in a suspended one. A job that ends meanwhile was measured by its release."""
        group = job.group
        if group is None:
            return
        if job.sched.state == "suspended":
            await asyncio.to_thread(group.suspend)
            return
        if job.sched.state != "running":
            return
        try:
            usage = await asyncio.to_thread(group.usage)
        except OSError:
            if job.group is group:
                raise
            return  # It ended meanwhile, and its cgroup went with it.
        if job.group is not group:
            return
        job.sched.observed_memory, job.sched.observed_commit = usage.private_memory, usage.committed_memory
        for session_group in list(job.borrowed):
            borrowed = await asyncio.to_thread(session_group.usage)
            job.sched.observed_memory += borrowed.private_memory
            job.sched.observed_commit += borrowed.committed_memory
        job.peak_committed = max(job.peak_committed, usage.peak_committed_memory)
        job.row["peak_private_memory"] = max(job.row.get("peak_private_memory") or 0, group.peak_private_memory)

    async def _limit_memory(self, job: ServiceJob) -> None:
        """Stop a running job one of whose Lean processes holds more than `process_memory_gib`: it
        ends unavailable (`_stopped`) instead of taking the machine's memory, which stops every
        other check and queues it again, itself included at the head. A resident session that
        holds such a process ends with it: its next check starts a new one."""
        limit = int(self.config.process_memory_gib * GIB)
        group, task = job.group, job.task
        if (not limit or group is None or task is None or task.done() or job.memory_limited is not None
                or job.sched.state != "running"):
            return
        held = {each: await asyncio.to_thread(each.largest_process) for each in (group, *job.borrowed)}
        largest = max(held.values())
        if largest <= limit or job.group is not group:
            return
        logger.warning("Lean job %s is stopped: one of its processes holds %.1f GiB, past the limit of %g GiB",
                       job.id, largest / GIB, self.config.process_memory_gib)
        job.memory_limited = largest
        for session_group in job.borrowed:
            if held.get(session_group, 0) > limit:
                await asyncio.to_thread(session_group.terminate)
        task.cancel()

    async def _sample_loop(self) -> None:
        last_sample = last_prune = 0.0
        while True:
            slept = time.monotonic()
            await asyncio.sleep(TICK_SECONDS)
            self.loop_lag += max(0.0, time.monotonic() - slept - TICK_SECONDS)
            try:
                now = self.clock()
                for job in list(self.jobs.values()):
                    try:
                        await self._sample_job(job)
                        await self._limit_memory(job)
                    except Exception:
                        # One job's failed measurement costs neither the others' nor the rest
                        # of this tick: in the Linux pilot half the checks were never measured.
                        logger.exception("Measuring Lean job %s failed; it is retried", job.id)
                    if (job.sched.lane == "interactive" and not job.abandoned and not job.done.is_set()
                            and now - (job.row.get("last_polled_at") or now) > CLIENT_SECONDS):
                        job.abandoned = True  # Its client has gone.
                        asyncio.create_task(self.cancel(job.id))
                await self.sessions.evict()
                low = self._last.get("memory_available")
                if low is not None and low < self.config.low_watermark_gib * GIB:
                    await self.sessions.evict(one=True)  # Memory comes first: an idle session can start again.
                if now - last_sample >= SAMPLE_SECONDS:
                    last_sample = now
                    self._last["cpu_busy"] = self._cpu.busy()
                    jobs = list(self.jobs.values())
                    await asyncio.to_thread(
                        self.store.sample, at=now, cpu_busy=self._last["cpu_busy"],
                        memory_available=self._last["memory_available"],
                        commit_available=self._last["commit_available"],
                        threads_in_use=sum(job.sched.assigned for job in jobs if job.sched.state == "running"),
                        running=sum(job.sched.state == "running" for job in jobs),
                        queued=sum(job.sched.state == "queued" for job in jobs),
                        suspended=sum(job.sched.state == "suspended" for job in jobs))
                if now - last_prune >= 3600:
                    last_prune = now
                    await asyncio.to_thread(self.store.prune, now)
            except Exception:
                logger.exception("Lean service measurement failed; it is retried")
