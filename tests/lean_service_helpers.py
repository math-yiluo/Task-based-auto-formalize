"""Lean services for tests: the real core in this process, or checks run at once."""
from __future__ import annotations

from uuid import uuid4

from formal_geyser.lean_service.executor import JobSpec, run_check


class LocalClient:
    """The Lean service client interface over an in-process LeanService, without HTTP."""

    def __init__(self, service):
        self.service = service

    async def submit(self, spec):
        return await self.service.submit(spec)

    async def wait(self, job_id, timeout):
        return await self.service.wait(job_id, timeout)

    async def cancel(self, job_id):
        return await self.service.cancel(job_id)

    async def status(self):
        return self.service.status()


class DirectLeanService:
    """Runs each check at once, in this process, with a synthetic runner: no scheduling,
    no cgroups, no sandbox. The test suite's default Lean service (see conftest)."""

    def __init__(self, runner=None):
        from tests.conftest import FakeRunner

        self.runner = runner or FakeRunner()
        self.specs: list[JobSpec] = []

    async def submit(self, spec):
        parsed = JobSpec.model_validate(spec)
        self.specs.append(parsed)
        result = await run_check(parsed.request, runner=self.runner, threads=1)
        return {"id": uuid4().hex, "lane": parsed.lane, "owner": parsed.owner, "label": parsed.label,
                "state": "done", "outcome": "completed", "result": result, "deduplicated": False}

    async def wait(self, job_id, timeout):
        raise AssertionError("Direct checks are done when submitted")

    async def cancel(self, job_id):
        return {"id": job_id, "state": "done", "outcome": "cancelled"}

    async def status(self):
        return {"counts": {"running": 0, "queued": 0, "suspended": 0}, "jobs": [], "threads": 1,
                "threads_in_use": 0}
