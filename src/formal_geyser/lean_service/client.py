"""How TaskHub, the command line and project setup use the Lean service.

`check` submits one check and waits for it. A development check is cancelled with its
caller. An official check is not: if its caller stops waiting (a pause, a restart, or the
bounded wait of `waiting`), the job continues in the service and a later identical request
attaches to it or reuses its result. The service is started on demand and keeps running on
its own; `connect` returns the project's client, and tests install another with `install`.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .. import cgroups
from ..coordination.errors import ValidationContinues
from ..coordination.local_operator import bearer
from ..registry import ApprovalRequired, EnvironmentUnavailable, RegistryError

START_SECONDS = 90.0
POLL_SECONDS = 25.0
WAIT: ContextVar[float | None] = ContextVar("lean_service_wait", default=None)
# Official checks a review or a publication waits for run before other official checks.
BLOCKING: ContextVar[bool] = ContextVar("lean_service_blocking", default=True)
ERRORS: dict[str, type[Exception]] = {
    "ApprovalRequired": ApprovalRequired, "EnvironmentUnavailable": EnvironmentUnavailable,
    "RegistryError": RegistryError, "ValidationError": RegistryError, "ValueError": ValueError,
    "KeyError": KeyError, "TypeError": TypeError, "OSError": OSError, "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
}


class LeanJobPending(ValidationContinues):
    """An official check continues in the Lean service after its caller stopped waiting."""

    def __init__(self, job: dict[str, Any]):
        state = job.get("state")
        where = f"queued at position {job['position']}" if state == "queued" and job.get("position") else state
        super().__init__(f"The Lean service is still running this check ({where})",
                         {key: job.get(key) for key in ("id", "state", "position", "lane", "label")})
        self.job = job


class LeanServiceSlow(ValidationContinues):
    """The Lean service did not answer an official check in time. A check it received goes on
    there, and asking for the same check again attaches to it or to its result (one job per
    content); one it never received is submitted then. In the Linux pilot such timeouts left
    ten candidates as `uncertain`, waiting for an operator although their checks had finished."""

    def __init__(self, spec: dict, job: dict | None = None):
        super().__init__("The Lean service did not answer in time; asking again attaches to this check",
                         {"id": (job or {}).get("id"), "state": (job or {}).get("state", "unknown"),
                          "lane": spec.get("lane"), "label": spec.get("label")})


def _timed_out(error: BaseException) -> bool:
    import httpx

    return isinstance(error, httpx.TimeoutException)


class ServiceUnavailable(EnvironmentUnavailable):
    pass


@contextmanager
def waiting(seconds: float | None):
    """Official checks started inside wait at most this long before LeanJobPending."""
    token = WAIT.set(seconds)
    try:
        yield
    finally:
        WAIT.reset(token)


@contextmanager
def priority(*, blocking: bool):
    """Official checks started inside block a review or a publication, or do not."""
    token = BLOCKING.set(blocking)
    try:
        yield
    finally:
        BLOCKING.reset(token)


def state_directory(root: Path) -> Path:
    return Path(root).resolve() / ".task-based" / "lean-service"


def supervisor_running(root: Path) -> bool:
    """Whether a supervisor holds this project's service (its lock)."""
    import portalocker

    path = state_directory(root) / "supervisor.lock"
    if not path.exists():
        return False
    try:
        with portalocker.Lock(str(path), mode="a+b", timeout=0):
            return False
    except portalocker.exceptions.LockException:
        return True


def start_supervisor(root: Path) -> None:
    """Start the service's supervisor outside this process tree: it outlives TaskHub. This is an
    explicit start, so it clears an earlier stop request."""
    state_directory(root).mkdir(parents=True, exist_ok=True)
    (state_directory(root) / "stop").unlink(missing_ok=True)
    # Its own scope of the user's systemd manager: stopping TaskHub's unit does not end it.
    command = cgroups.scope_command(
        f"formal-geyser-lean-supervisor-{uuid4().hex}.scope",
        [sys.executable, "-m", "formal_geyser.lean_service", "supervise", "--project", str(Path(root).resolve())])
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, cwd=str(Path(root).resolve()), env=cgroups.bus_environment(),
                     start_new_session=True)


class HttpClient:
    """The Lean service of one project, over loopback HTTP."""

    def __init__(self, root: Path, *, start: Callable[[Path], None] = start_supervisor):
        self.root = Path(root).resolve()
        self.state = state_directory(self.root)
        self._start = start
        self._starting: asyncio.Lock | None = None

    def _address(self) -> str | None:
        try:
            record = json.loads((self.state / "service.json").read_text(encoding="utf-8"))
            return f"http://127.0.0.1:{int(record['port'])}"
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def _request(self, method: str, path: str, body: dict | None = None, *, timeout: float = 30.0,
                       start: bool = True):
        """`start`: start the service if it does not answer; an observer never does."""
        import httpx

        for attempt in range(3):
            address = self._address()
            if address is None:
                if not start:
                    raise ServiceUnavailable("The Lean service is not running")
                await self.ensure_running()
                continue
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                             headers=bearer(self.root, "lean-service")) as client:
                    response = await client.request(method, address + path, json=body)
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectTimeout):
                if attempt == 2 or not start:
                    raise ServiceUnavailable("The Lean service does not answer")
                await self.ensure_running()
                continue
            if response.status_code == 404:
                raise KeyError(path)
            if response.status_code >= 400:
                raise ServiceUnavailable(f"The Lean service refused {path}: {response.text[:500]}")
            return response.json()
        raise ServiceUnavailable("The Lean service did not start")

    async def healthy(self) -> bool:
        import httpx

        address = self._address()
        if address is None:
            return False
        try:
            async with httpx.AsyncClient(timeout=3.0, trust_env=False,
                                         headers=bearer(self.root, "lean-service")) as client:
                response = await client.get(address + "/v1/health")
            return response.status_code == 200 and response.json().get("ok") is True
        except (httpx.HTTPError, ValueError):
            return False

    async def ensure_running(self) -> None:
        if await self.healthy():
            return
        if self._starting is None:
            self._starting = asyncio.Lock()
        async with self._starting:
            if await self.healthy():
                return
            # A running supervisor starts the server again itself (after a restart, say);
            # a second one would only wait for its lock.
            if not await asyncio.to_thread(supervisor_running, self.root):
                await asyncio.to_thread(self._start, self.root)
            deadline = time.monotonic() + START_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if await self.healthy():
                    return
        raise ServiceUnavailable("The Lean service did not start; see .task-based/lean-service/service.log")

    async def submit(self, spec: dict) -> dict:
        return await self._request("POST", "/v1/jobs", spec, timeout=120.0)

    async def wait(self, job_id: str, timeout: float) -> dict:
        return await self._request("POST", f"/v1/jobs/{job_id}/wait", {"timeout": timeout}, timeout=timeout + 60.0)

    async def cancel(self, job_id: str) -> dict:
        return await self._request("POST", f"/v1/jobs/{job_id}/cancel", {}, timeout=120.0)

    async def status(self, *, start: bool = True) -> dict:
        return await self._request("GET", "/v1/status", start=start)

    async def telemetry(self, hours: float) -> dict:
        return await self._request("GET", f"/v1/telemetry?hours={hours}", start=False)

    async def shutdown(self) -> dict:
        return await self._request("POST", "/v1/shutdown", {}, start=False)


_FACTORY: Callable[[Path], Any] | None = None
_CLIENTS: dict[Path, HttpClient] = {}


def connect(root: Path):
    """The project's Lean service client."""
    if _FACTORY is not None:
        return _FACTORY(Path(root))
    key = Path(root).resolve()
    if key not in _CLIENTS:
        _CLIENTS[key] = HttpClient(key)
    return _CLIENTS[key]


def install(factory: Callable[[Path], Any] | None) -> Callable[[Path], Any] | None:
    """Use another client for every project (tests run the executor in their own process)."""
    global _FACTORY
    previous, _FACTORY = _FACTORY, factory
    return previous


async def current_status(root: Path) -> dict | None:
    """The service's status when it runs; reading it never starts the service."""
    connection = connect(root)
    try:
        if isinstance(connection, HttpClient):
            return await asyncio.wait_for(connection.status(start=False), 3.0)
        return await asyncio.wait_for(connection.status(), 3.0)
    except (ServiceUnavailable, TimeoutError, KeyError):
        return None


def raise_for(result: dict) -> dict:
    error = result.get("error")
    if error is not None:
        raise ERRORS.get(error.get("type"), RuntimeError)(error.get("message", "The Lean check failed"))
    return result


async def run(client, spec: dict, *, wait: float | None = None) -> dict:
    """Submit and wait; the result, with the check's own error raised as it was raised there.
    An official check whose request times out continues in the service (LeanServiceSlow)."""
    official = spec["lane"] == "official"
    try:
        job = await client.submit(spec)
    except Exception as error:
        if official and _timed_out(error):
            raise LeanServiceSlow(spec) from error
        raise
    deadline = None if wait is None else time.monotonic() + wait
    resubmitted = 0
    try:
        while job["state"] != "done":
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise LeanJobPending(job)
            try:
                job = await client.wait(job["id"], POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining))
            except KeyError:
                # The service lost the job (its records were reset): ask again. An official check
                # of the same content is one job; never mistake this for a finding.
                resubmitted += 1
                if resubmitted > 3:
                    raise ServiceUnavailable("The Lean service keeps losing this check") from None
                job = await client.submit(spec)
            except Exception as error:
                if official and _timed_out(error):
                    raise LeanServiceSlow(spec, job) from error
                raise
    except asyncio.CancelledError:
        if spec["lane"] == "interactive":
            # A development check belongs to its caller; an official one goes on.
            cancel = asyncio.ensure_future(client.cancel(job["id"]))
            await asyncio.gather(cancel, return_exceptions=True)
        raise
    if job.get("outcome") != "completed" or "result" not in job:
        raise EnvironmentUnavailable(f"The Lean service ended this check: {job.get('outcome')}")
    return raise_for(job["result"])


async def check(root: Path, environment, blobs, *, modules: Iterable[str], lane: str, owner: str, label: str,
                snapshot: str | None = None, directory: Path | None = None, overlay: Path | None = None,
                kernel_replay: bool = True, inspections: Iterable[dict] = ()) -> dict:
    """One check in the project's Lean service: {doctor, compilation, inspections, ...}."""
    blocking = lane == "official" and BLOCKING.get()
    spec = {"kind": "check", "lane": lane, "owner": owner, "label": label[:500], "blocking": blocking,
            "request": {"environment": environment.model_dump(mode="json"), "blobs": str(Path(blobs.root).resolve()),
                        "snapshot": snapshot, "directory": None if directory is None else str(Path(directory).resolve()),
                        "overlay": None if overlay is None else str(Path(overlay).resolve()),
                        "modules": list(modules), "kernel_replay": kernel_replay,
                        "inspections": [dict(item) for item in inspections]}}
    return await run(connect(root), spec, wait=WAIT.get() if lane != "interactive" else None)
