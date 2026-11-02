"""Claude subscription profiles, used one at a time (account_profiles describes the rotation).

Each profile is a Claude Code configuration directory in which the operator signed in once with
``/login``: the first is ``claude.home`` itself, each other one ``claude.home/profiles/<name>``.
Everything a conversation keeps in that directory (Claude Code's transcripts, TaskHub's session
records, the per-session shell state) lives once, in the first profile's directory; the others
link to it. Claude uses the first usable profile in the operator's order of preference; when the
one in use reaches its 5-hour or weekly limit, Claude moves to the next one, and while every
profile is at its limit, new Claude sessions wait and start again by themselves at the earliest
reset.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import account_profiles as accounts
from .account_profiles import (  # noqa: F401  (the names this module has always offered)
    DEFAULT_PROFILE,
    FILES_SECONDS,
    HISTORY_KEPT,
    HISTORY_SHOWN,
    UNKNOWN_RESET_MS,
    USAGE_STATUSES,
    AccountProfiles,
    ProfileKind,
    ProfileLayoutError,
)
from .models import ClaudeConfig

CLAUDE_PROFILES_SCHEMA = accounts.profiles_schema("claude")
# What a conversation keeps in Claude Code's configuration directory: transcripts (projects),
# TaskHub's session records (task-based), and per-session shell and task state.
SHARED_DIRECTORIES = ("projects", "task-based", "session-env", "sessions", "shell-snapshots", "todos",
                      "file-history")
LIMIT_TYPES = {"five_hour": "5h", "overage": "overage"}
_EPOCH = re.compile(r"resets at (\d{9,12})")
_TYPE = re.compile(r"\(([a-z_]+)\)")
_CLOCK = re.compile(r"resets\s+(?:(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)\s*\((?P<zone>[^)]+)\)", re.I)
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _claude_reset(detail: str, now_ms: int) -> tuple[int | None, str]:
    """When a reported limit resets and which limit it is: from the reset time Claude Code
    reports with a rate limit, else from its message ("resets 1:30pm (Zone)"); None without one."""
    text = detail or ""
    kind = next((LIMIT_TYPES.get(token, "weekly" if token.startswith("seven_day") else token)
                 for token in _TYPE.findall(text) if token in LIMIT_TYPES or token.startswith("seven_day")), None)
    if kind is None:
        lowered = text.lower()
        kind = "weekly" if "week" in lowered else "5h" if "session limit" in lowered or "5-hour" in lowered else "unknown"
    until = None
    if match := _EPOCH.search(text):
        until = int(match.group(1)) * 1000
    elif match := _CLOCK.search(text):
        until = _clock_reset(match, now_ms)
    return until, kind


def _clock_reset(match: re.Match, now_ms: int) -> int | None:
    try:
        zone = ZoneInfo(match.group("zone").strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(match.group("hour")) % 12 + (12 if match.group("half").lower() == "pm" else 0)
    minute = int(match.group("minute") or 0)
    now = dt.datetime.fromtimestamp(now_ms / 1000, zone)
    if match.group("month"):
        month = _MONTHS.get(match.group("month").lower())
        if month is None:
            return None
        moment = now.replace(month=month, day=int(match.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
        if moment < now - dt.timedelta(days=1):
            moment = moment.replace(year=moment.year + 1)
    else:
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment <= now:
            moment += dt.timedelta(days=1)
    return int(moment.timestamp() * 1000)


def parse_limit(detail: str, now_ms: int) -> tuple[int, str]:
    """When a reported limit resets (epoch milliseconds) and which limit it is; an hour from now
    when the report does not say."""
    until, kind = _claude_reset(detail, now_ms)
    return max(until if until is not None else now_ms + UNKNOWN_RESET_MS, now_ms + 60_000), kind


CLAUDE = ProfileKind(backend="claude", label="Claude", sign_in=".credentials.json", shared=SHARED_DIRECTORIES,
                     implicit=True, parse=_claude_reset,
                     sign_in_command="CLAUDE_CONFIG_DIR={home} claude, then /login")
accounts.PROFILE_KINDS["claude"] = CLAUDE


def profile_names(config: ClaudeConfig) -> list[str]:
    """The profiles in their order of preference; none without a configured home."""
    return accounts.profile_names(CLAUDE, config)


def profile_home(config: ClaudeConfig, name: str) -> Path:
    return accounts.profile_home(CLAUDE, config, name)


def profile_homes(config: ClaudeConfig) -> list[str]:
    return accounts.profile_homes(CLAUDE, config)


def prepare_profile(config: ClaudeConfig, name: str) -> Path:
    """Make the profile's directory, linking its conversation storage to the first profile's."""
    return accounts.prepare_profile(CLAUDE, config, name)


def profile_files(config: ClaudeConfig) -> dict[str, dict[str, Any]]:
    """Each profile's directory, prepared: whether it holds a sign-in, and any layout problem."""
    return accounts.profile_files(CLAUDE, config)


class ClaudeProfiles(AccountProfiles):
    async def refresh_claude_profiles(self) -> dict[str, dict[str, Any]]:
        return await self.refresh_profiles(CLAUDE)

    async def claude_session_profile(self) -> dict[str, str] | None:
        """The profile a Claude session starts or continues with now, its directory prepared."""
        return await self.session_profile(CLAUDE)

    async def claude_limit_reached(self, profile: str, detail: str, *, subject_id=None, worker_id=None) -> None:
        """Record that a profile reached a usage limit; if it is the selected one, move on."""
        await self.profile_limit_reached(CLAUDE, profile, detail, worker_id=worker_id)

    async def claude_usage_observed(self, profile: str, info: dict[str, Any]) -> None:
        """What Claude Code reported of a profile's usage window when its state changed (within the
        limit, approaching it, or at it): the share used and when the window resets."""
        window, status = info.get("rateLimitType"), info.get("status")
        if not isinstance(window, str) or not window or status not in USAGE_STATUSES:
            return
        utilization = info.get("utilization")
        utilization = float(utilization) if isinstance(utilization, (int, float)) and 0 <= utilization <= 1 else None
        resets = info.get("resetsAt")
        resets_at = int(resets) * 1000 if isinstance(resets, int) and resets > 0 else None
        await self.profile_usage_observed(CLAUDE, profile, [
            {"window": window, "status": status, "utilization": utilization, "resets_at": resets_at}])

    def _claude_profiles_hold(self, c) -> dict[str, Any] | None:
        return self._profiles_hold(c, CLAUDE)

    def _claude_profiles_view(self, c) -> dict[str, Any] | None:
        return self._profiles_view(c, CLAUDE)

    async def claude_profiles(self) -> dict[str, Any] | None:
        return await self.profiles_report(CLAUDE)

    async def claude_profile_summary(self) -> dict[str, Any] | None:
        """The profile in use, for the dashboard's header; SQL only."""
        return await self.profile_summary(CLAUDE)

    def _claude_profile_command(self, c, name: str, p: dict, actor) -> dict[str, Any] | None:
        return self._profile_command(c, CLAUDE, name, p, actor)
