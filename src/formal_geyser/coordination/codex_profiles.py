"""Codex accounts, used one at a time (account_profiles describes the rotation).

Each profile is a Codex home (CODEX_HOME) in which the operator signed in once with ``codex login``:
the first is ``codex.home`` itself, each other one ``codex.home/profiles/<name>``. Every profile's
sessions use the first one's databases (``sqlite_home``), and each profile's session files, shell
snapshots and thread locks link to the first one's, so a thread continues under whichever profile
is in use. A profile keeps only its own sign-in (``auth.json``), its list of trusted projects
(``config.toml``) and scratch files. Workers, reviewers, the Integrator and the Dispatcher all run
on the profile in use.

Without configured profiles Codex uses its home alone, and a usage limit holds Codex until the
operator switches the account and resumes, as before.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import account_profiles as accounts
from .account_profiles import AccountProfiles, ProfileCheck, ProfileKind
from .models import CodexConfig

CODEX_PROFILES_SCHEMA = accounts.profiles_schema("codex")
# What a thread keeps in its Codex home besides the databases: its session file, the shell
# snapshots of its commands, and the lock that keeps one writer per thread.
SHARED_DIRECTORIES = ("sessions", "archived_sessions", "shell_snapshots", "thread-writer-locks")
# "You've hit your usage limit. ... try again at Nov 1st, 2026 11:09 PM." or, on the same day,
# "... try again at 11:09 PM.": Codex names the moment in this machine's local time, to the minute.
_RETRY = re.compile(r"try again at (?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:st|nd|rd|th)?, (?P<year>\d{4}) )?"
                    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<half>AM|PM)", re.I)
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# The usage windows' names, as the limits' kinds: Codex's 5-hour and weekly windows.
WINDOWS = {300: "5h", 10080: "weekly"}
# How long an account check may take: starting Codex and one read of the account's usage.
CHECK_TIMEOUT = 45.0


def _codex_reset(detail: str, now_ms: int) -> tuple[int | None, str]:
    """When a usage limit Codex reported resets (epoch milliseconds), at the end of the minute it
    names; None without one. Codex does not say which window it is."""
    match = _RETRY.search(detail or "")
    if match is None:
        return None, "unknown"
    hour = int(match.group("hour")) % 12 + (12 if match.group("half").upper() == "PM" else 0)
    minute = int(match.group("minute"))
    try:
        if match.group("month"):
            month = _MONTHS.get(match.group("month").lower())
            if month is None:
                return None, "unknown"
            moment = dt.datetime(int(match.group("year")), month, int(match.group("day")), hour, minute)
        else:
            moment = dt.datetime.fromtimestamp(now_ms / 1000).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment.timestamp() * 1000 <= now_ms:
                moment += dt.timedelta(days=1)
    except ValueError:
        return None, "unknown"
    return int(moment.timestamp() * 1000) + 60_000, "unknown"


def _window_name(minutes: Any, slot: str) -> str:
    if type(minutes) is not int or minutes <= 0:
        return slot
    if minutes in WINDOWS:
        return WINDOWS[minutes]
    return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}min"


def codex_usage(snapshot: Any) -> list[dict[str, Any]]:
    """The usage windows of a Codex rate-limit report (``account/rateLimits/updated``): each window
    by its length, the share used and when it resets; a bucket other than Codex's own (a model's)
    is named with it. A window at 100% is at its limit."""
    if not isinstance(snapshot, dict):
        return []
    bucket = snapshot.get("limitId")
    windows = []
    for slot in ("primary", "secondary"):
        window = snapshot.get(slot)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        name = _window_name(window.get("windowDurationMins"), slot)
        if isinstance(bucket, str) and bucket and bucket != "codex":
            name = f"{bucket} {name}"
        resets = window.get("resetsAt")
        windows.append({"window": name, "status": "rejected" if used >= 100 else "allowed",
                        "utilization": min(max(used / 100, 0.0), 1.0),
                        "resets_at": int(resets) * 1000 if type(resets) is int and resets > 0 else None})
    return windows


CODEX = ProfileKind(backend="codex", label="Codex", sign_in="auth.json", shared=SHARED_DIRECTORIES,
                    implicit=False, parse=_codex_reset,
                    sign_in_command="CODEX_HOME={home} codex login --device-auth")
accounts.PROFILE_KINDS["codex"] = CODEX


def profile_names(config: CodexConfig) -> list[str]:
    """The profiles in their order of preference; none without configured profiles."""
    return accounts.profile_names(CODEX, config)


def profile_home(config: CodexConfig, name: str) -> Path:
    return accounts.profile_home(CODEX, config, name)


def profile_homes(config: CodexConfig) -> list[str]:
    return accounts.profile_homes(CODEX, config)


def prepare_profile(config: CodexConfig, name: str) -> Path:
    return accounts.prepare_profile(CODEX, config, name)


def profile_files(config: CodexConfig) -> dict[str, dict[str, Any]]:
    return accounts.profile_files(CODEX, config)


def sign_in_homes(config: CodexConfig) -> list[str]:
    """Every Codex home that may hold a sign-in: each profile's, else the configured home."""
    return profile_homes(config) or ([config.home] if config.home else [])


def account_check_result(reply: Any) -> ProfileCheck:
    """What a Codex account's usage read (account/rateLimits/read) says: whether it can be used
    now (Codex's own answer, ordinaryUsageAllowed; without one, whether a window is at 100%; None
    when neither says), until when it cannot (the last reset of a window at its limit, else of any
    window), and its usage windows."""
    if not isinstance(reply, dict):
        return None, None, []
    windows = codex_usage(reply.get("rateLimits"))
    allowed = reply.get("ordinaryUsageAllowed")
    if not isinstance(allowed, bool):
        allowed = (not any(item["status"] == "rejected" for item in windows)) if windows else None
    if allowed is not False:
        return allowed, None, windows
    exhausted = [item["resets_at"] for item in windows if item["status"] == "rejected" and item["resets_at"]]
    resets = exhausted or [item["resets_at"] for item in windows if item["resets_at"]]
    return False, max(resets) if resets else None, windows


async def read_account_limits(executable: str, home: str, storage: str, *, timeout: float = CHECK_TIMEOUT) -> Any:
    """The usage of the account Codex is signed in to from `home` (account/rateLimits/read), read
    by a short app-server of its own that uses the profiles' shared databases (`storage`). It sends
    no model input and bills nothing."""
    from .runtime.executable import resolve_codex_executable

    process = await asyncio.create_subprocess_exec(
        resolve_codex_executable(executable), "-c", "sqlite_home=" + json.dumps(storage), "app-server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "CODEX_HOME": home, "CODEX_SQLITE_HOME": storage}, limit=16 * 2**20)
    assert process.stdin is not None and process.stdout is not None
    stdin, stdout = process.stdin, process.stdout

    async def send(message: dict[str, Any]) -> None:
        stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await stdin.drain()

    async def request(number: int, method: str, params: dict[str, Any]) -> Any:
        await send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        while True:
            line = await stdout.readline()
            if not line:
                raise ConnectionError("Codex ended before it answered")
            message = json.loads(line)
            if isinstance(message, dict) and message.get("id") == number and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"{method}: {str(message['error'])[:200]}")
                return message.get("result")

    async def exchange() -> Any:
        await request(1, "initialize", {"clientInfo": {"name": "formal-geyser", "title": "FormalGeyser", "version": "0.1.0"}})
        await send({"jsonrpc": "2.0", "method": "initialized"})
        return await request(2, "account/rateLimits/read", {})
    try:
        return await asyncio.wait_for(exchange(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


class CodexProfiles(AccountProfiles):
    # How an account check reads a profile's usage, given its directory (read_account_limits);
    # tests replace it.
    codex_limits_reader: Callable[[str], Awaitable[Any]] | None = None

    def _profile_check(self, kind: ProfileKind) -> Any:
        """Before Codex conversations move to a profile, its account's usage is read: in the Linux
        pilot 40 reviewers moved together to an account at its weekly limit, and each failed on it,
        then on the next one."""
        if kind is not CODEX:
            return super()._profile_check(kind)
        config = self.config.codex

        async def default(home: str) -> Any:
            return await read_account_limits(config.executable, home, str(Path(str(config.home)).expanduser()))
        reader = self.codex_limits_reader or default

        async def check(name: str, home: str) -> ProfileCheck:
            return account_check_result(await reader(home))
        return check

    async def refresh_codex_profiles(self) -> dict[str, dict[str, Any]]:
        return await self.refresh_profiles(CODEX)

    async def codex_session_profile(self) -> dict[str, str] | None:
        """The profile a Codex session starts or continues with now, its directory prepared; None
        without configured profiles."""
        return await self.session_profile(CODEX)

    async def codex_limit_reached(self, profile: str, detail: str, *, subject_id=None, worker_id=None) -> None:
        """Record that a profile reached a usage limit; if it is the selected one, move on."""
        await self.profile_limit_reached(CODEX, profile, detail, worker_id=worker_id)

    async def codex_usage_observed(self, profile: str, snapshot: Any) -> None:
        """What Codex reported of a profile's usage windows (account/rateLimits/updated)."""
        await self.profile_usage_observed(CODEX, profile, codex_usage(snapshot))

    def _codex_profiles_view(self, c) -> dict[str, Any] | None:
        return self._profiles_view(c, CODEX)

    async def codex_profiles(self) -> dict[str, Any] | None:
        return await self.profiles_report(CODEX)

    async def codex_profile_summary(self) -> dict[str, Any] | None:
        """The profile in use, for the dashboard's header; SQL only."""
        return await self.profile_summary(CODEX)

    def _codex_profile_command(self, c, name: str, p: dict, actor) -> dict[str, Any] | None:
        return self._profile_command(c, CODEX, name, p, actor)
