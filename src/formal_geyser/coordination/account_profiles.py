"""Subscription sign-ins used one at a time, for each backend (Claude Code and Codex).

Each profile is a configuration directory in which the operator signed in once: the first is the
backend's home itself, each other one ``home/profiles/<name>``. What a conversation keeps there
lives once, in the first profile's directory; the others link to it, so a conversation continues
unchanged under whichever profile is in use. (Codex keeps its databases in the first profile's
directory by itself, through ``sqlite_home``.) Only a profile's own sign-in and account state stay
in its directory.

Sessions stay on the selected profile while it can be used: signed in, in the rotation, and not at
a usage limit. A profile ahead of it in the operator's order of preference that can be used again
does not take them back. When the selected profile reaches a usage limit, a backend with an account
check (Codex) walks the rotation from the top of the order of preference and checks each profile,
whatever limit and reset time are recorded for it (an account is often refreshed before the time it
named), and moves to the first the check finds usable; each found at its limit records the reset the
check reports. While every profile is found at its limit, the backend's new sessions wait, and the
walk is tried again after WALK_RETRY_MS. A backend without a check (Claude) moves to the first
profile its recorded limits allow, and while all are at their limit waits for the earliest recorded
reset. The order is the configured one until the operator changes it, at any time. The operator may
also select a profile by hand, which clears its recorded limit and puts it first.

The operator may also take a profile out of the rotation (an account kept for other use): it is
never selected, and when it is the one in use, the next usable one is selected at once. While no
profile in the rotation can be used, the backend's sessions wait for the operator.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import HubError
from .models import Role

logger = logging.getLogger(__name__)

# How many limit and selection records each backend's tab keeps, and shows.
HISTORY_KEPT = 200
HISTORY_SHOWN = 20
USAGE_STATUSES = ("allowed", "allowed_warning", "rejected")
# The name of the only profile when none are configured.
DEFAULT_PROFILE = "default"
# A limit reported without a reset time is tried again after this long.
UNKNOWN_RESET_MS = 3_600_000
# How long a reading of the profiles' directories is used before it is read again.
FILES_SECONDS = 30.0
# The kinds of limit the tabs name.
LIMIT_KINDS = ("5h", "weekly", "overage")
# How long an account check that found a profile usable answers for it.
CHECK_SECONDS = 300.0
# While every profile in the rotation was found at its limit, the walk through them is tried again
# after this long (milliseconds); new sessions wait meanwhile.
WALK_RETRY_MS = 1_800_000
# What a backend's account check says of a profile (see AccountProfiles._profile_check): whether it
# can be used now (None: the check could not say), until when it cannot, and its usage windows.
ProfileCheck = tuple[bool | None, int | None, list[dict[str, Any]]]


def profiles_schema(backend: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {backend}_profile_limits(
 profile TEXT PRIMARY KEY, limited_until INTEGER NOT NULL, limit_type TEXT NOT NULL,
 detail TEXT NOT NULL, reported_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS {backend}_profile_selection(
 id INTEGER PRIMARY KEY CHECK(id=1), profile TEXT NOT NULL, reason TEXT NOT NULL, selected_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS {backend}_profile_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, kind TEXT NOT NULL, profile TEXT,
 limit_type TEXT, until INTEGER, previous TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS {backend}_profile_usage(
 profile TEXT NOT NULL, limit_window TEXT NOT NULL, status TEXT NOT NULL, utilization REAL, resets_at INTEGER,
 observed_at INTEGER NOT NULL, PRIMARY KEY(profile,limit_window));
CREATE TABLE IF NOT EXISTS {backend}_profile_excluded(profile TEXT PRIMARY KEY, excluded_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS {backend}_profile_priority(profile TEXT PRIMARY KEY, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS {backend}_profile_walk(
 id INTEGER PRIMARY KEY CHECK(id=1), all_limited_at INTEGER NOT NULL, retry_at INTEGER NOT NULL);
"""


@dataclass(frozen=True)
class ProfileKind:
    """What differs between the backends' profiles."""
    backend: str
    label: str
    sign_in: str  # The file that holds a profile's sign-in.
    shared: tuple[str, ...]  # Directories kept once, in the first profile's directory.
    implicit: bool  # Without configured profiles, the home is the one profile.
    # When a usage limit that the backend reported resets (epoch milliseconds, None when the
    # report does not say) and which limit it is.
    parse: Callable[[str, int], tuple[int | None, str]]
    sign_in_command: str  # How to sign a profile in, given its directory as {home}.


class ProfileLayoutError(RuntimeError):
    """A profile's directory keeps conversation storage of its own instead of the shared one."""


def profile_names(kind: ProfileKind, config: Any) -> list[str]:
    """The profiles in their order of preference; none without a configured home."""
    if not config.home:
        return []
    return list(config.profiles) or ([DEFAULT_PROFILE] if kind.implicit else [])


def profile_home(kind: ProfileKind, config: Any, name: str) -> Path:
    names = profile_names(kind, config)
    if name not in names:
        raise ValueError(f"Unknown {kind.label} profile {name!r}")
    home = Path(str(config.home)).expanduser()
    return home if name == names[0] else home / "profiles" / name


def profile_homes(kind: ProfileKind, config: Any) -> list[str]:
    return [str(profile_home(kind, config, name)) for name in profile_names(kind, config)]


def prepare_profile(kind: ProfileKind, config: Any, name: str) -> Path:
    """Make the profile's directory, linking its conversation storage to the first profile's.
    An empty directory the backend made in place of a link is replaced; one with content is an
    error, since its conversations would otherwise be kept apart."""
    names = profile_names(kind, config)
    home, main = profile_home(kind, config, name), profile_home(kind, config, names[0])
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if name == names[0]:
        return home
    for entry in kind.shared:
        target, link = main / entry, home / entry
        target.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.resolve() != target.resolve():
                raise ProfileLayoutError(f"{link} links to {link.resolve()}, not to the shared {target}")
            continue
        if link.exists():
            if not link.is_dir() or any(link.iterdir()):
                raise ProfileLayoutError(f"{link} keeps conversations of its own: move its contents into {target} "
                                         "and remove it")
            link.rmdir()
        link.symlink_to(target, target_is_directory=True)
    return home


def profile_files(kind: ProfileKind, config: Any) -> dict[str, dict[str, Any]]:
    """Each profile's directory, prepared: whether it holds a sign-in, and any layout problem."""
    files = {}
    for name in profile_names(kind, config):
        problem = None
        try:
            home = prepare_profile(kind, config, name)
        except (ProfileLayoutError, OSError) as error:
            home, problem = profile_home(kind, config, name), str(error)
        files[name] = {"home": str(home), "signed_in": (home / kind.sign_in).is_file(), "problem": problem}
    return files


class AccountProfiles:
    """The rotation of one backend's profiles; ClaudeProfiles and CodexProfiles name the backend."""
    if TYPE_CHECKING:
        config: Any
        db: Any
        clock: Any
        _event: Any
        _role: Any

    def _profile_reads(self) -> dict[str, tuple[float, dict[str, dict[str, Any]]]]:
        """The last reading of each backend's profile directories (monotonic time, profile ->
        state); the coordination thread reads it, never the files themselves."""
        return self.__dict__.setdefault("_profile_files_read", {})

    def _profile_config(self, kind: ProfileKind) -> Any:
        return self.config.backend_config(kind.backend)

    async def refresh_profiles(self, kind: ProfileKind) -> dict[str, dict[str, Any]]:
        """Prepare and read the profiles' directories, off the coordination thread."""
        from .storage import collected_thread_call

        files = await collected_thread_call(profile_files, kind, self._profile_config(kind))
        self._profile_reads()[kind.backend] = (time.monotonic(), files)
        return files

    async def _fresh_profile_files(self, kind: ProfileKind) -> dict[str, dict[str, Any]]:
        cached = self._profile_reads().get(kind.backend)
        if cached is None or time.monotonic() - cached[0] > FILES_SECONDS:
            return await self.refresh_profiles(kind)
        return cached[1]

    def _profile_files_now(self, kind: ProfileKind) -> dict[str, dict[str, Any]]:
        cached = self._profile_reads().get(kind.backend)
        return cached[1] if cached else {}

    def _profile_limits(self, c, kind: ProfileKind, now: int) -> dict[str, dict[str, Any]]:
        return {row["profile"]: dict(row) for row in c.execute(
            f"SELECT * FROM {kind.backend}_profile_limits WHERE limited_until>?", (now,))}

    def _profile_priority(self, c, kind: ProfileKind) -> list[str]:
        """The profiles in the operator's order of preference: the order they set, else the
        configured one; a profile configured since comes last."""
        names = profile_names(kind, self._profile_config(kind))
        stored = [row["profile"] for row in c.execute(
            f"SELECT profile FROM {kind.backend}_profile_priority ORDER BY position")]
        ordered = [name for name in stored if name in names]
        return ordered + [name for name in names if name not in ordered]

    def _set_profile_priority(self, c, kind: ProfileKind, order: list[str]) -> None:
        c.execute(f"DELETE FROM {kind.backend}_profile_priority")
        c.executemany(f"INSERT INTO {kind.backend}_profile_priority(profile,position) VALUES(?,?)",
                      [(name, position) for position, name in enumerate(order)])
        self._event(c, f"{kind.backend}.profile_priority", f"{kind.backend}-profiles", {"order": order})

    def _selected_profile(self, c, kind: ProfileKind) -> str | None:
        names = profile_names(kind, self._profile_config(kind))
        row = c.execute(f"SELECT profile FROM {kind.backend}_profile_selection WHERE id=1").fetchone()
        # Before any selection, the first configured profile; _move_profile then selects the one
        # the order of preference names, and records it.
        return row["profile"] if row and row["profile"] in names else (names[0] if names else None)

    def _excluded_profiles(self, c, kind: ProfileKind) -> set[str]:
        """The profiles the operator took out of the rotation."""
        return {row["profile"] for row in c.execute(f"SELECT profile FROM {kind.backend}_profile_excluded")}

    @staticmethod
    def _profile_usable(name: str, limits: dict[str, Any], files: dict[str, dict[str, Any]],
                        excluded: set[str]) -> bool:
        # A profile not read yet counts as usable: its session reports what is wrong.
        state = files.get(name, {})
        return (name not in excluded and name not in limits and state.get("signed_in", True)
                and not state.get("problem"))

    def _record_profile_history(self, c, kind: ProfileKind, entry: str, profile: str | None, **fields: Any) -> None:
        """A limit or selection, for the backend's tab; only the latest HISTORY_KEPT are kept."""
        table = f"{kind.backend}_profile_history"
        c.execute(f"INSERT INTO {table}(at,kind,profile,limit_type,until,previous,reason) VALUES(?,?,?,?,?,?,?)",
                  (self.clock(), entry, profile, fields.get("limit_type"), fields.get("until"),
                   fields.get("previous"), fields.get("reason")))
        c.execute(f"DELETE FROM {table} WHERE id<=(SELECT MAX(id) FROM {table})-?", (HISTORY_KEPT,))

    def _select_profile(self, c, kind: ProfileKind, name: str, reason: str, previous: str | None) -> None:
        c.execute(f"INSERT INTO {kind.backend}_profile_selection(id,profile,reason,selected_at) VALUES(1,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET profile=excluded.profile,reason=excluded.reason,"
                  "selected_at=excluded.selected_at", (name, reason, self.clock()))
        self._event(c, f"{kind.backend}.profile_selected", f"{kind.backend}-profiles",
                    {"profile": name, "previous": previous, "reason": reason})
        self._record_profile_history(c, kind, "selected", name, previous=previous, reason=reason)

    def _move_profile(self, c, kind: ProfileKind, now: int, files: dict[str, dict[str, Any]], reason: str) -> str | None:
        """The selected profile while it can be used, by its recorded limits; else the first usable
        one in the order of preference, which becomes the selected one (`reason`: why the selected
        one is left). None when none can be used, and the selection stays. A profile ahead of a
        usable selected one is not moved to; before any selection, the first in the order is."""
        current = self._selected_profile(c, kind)
        if current is None:
            return None
        limits, excluded = self._profile_limits(c, kind, now), self._excluded_profiles(c, kind)
        chosen = c.execute(f"SELECT 1 FROM {kind.backend}_profile_selection WHERE id=1").fetchone() is not None
        usable = self._profile_usable(current, limits, files, excluded)
        if chosen and usable:
            return current
        preferred = next((name for name in self._profile_priority(c, kind)
                          if self._profile_usable(name, limits, files, excluded)), None)
        if preferred is None:
            return None
        if preferred != current:
            self._select_profile(c, kind, preferred, "priority" if usable else reason, current)
        return preferred

    async def session_profile(self, kind: ProfileKind) -> dict[str, str] | None:
        """The profile a session starts or continues with now, its directory prepared: the selected
        one while it can be used. When it cannot, a backend with an account check walks the
        rotation (_walk_profiles); one without moves to the first profile its recorded limits allow."""
        config = self._profile_config(kind)
        names = profile_names(kind, config)
        if not names:
            return None
        files = await self._fresh_profile_files(kind)

        def directory(name: str) -> dict[str, str]:
            return {"name": name, "home": files.get(name, {}).get("home") or str(profile_home(kind, config, name))}
        check = self._profile_check(kind)
        if check is None:
            def choose(c):
                # With none usable the dispatch hold keeps sessions from starting; one that started
                # before it still names the selected profile.
                return self._move_profile(c, kind, self.clock(), files, "unusable") or self._selected_profile(c, kind)
            return directory(await self.db.write(choose))

        def selected(c):
            now = self.clock()
            if c.execute(f"SELECT 1 FROM {kind.backend}_profile_selection WHERE id=1").fetchone() is None:
                # The first selection: the top of the order, which the check and the walk then judge.
                c.execute(f"INSERT INTO {kind.backend}_profile_selection(id,profile,reason,selected_at) "
                          "VALUES(1,?,'priority',?)", (self._profile_priority(c, kind)[0], now))
            current = self._selected_profile(c, kind) or names[0]
            usable = self._profile_usable(current, self._profile_limits(c, kind, now), files,
                                          self._excluded_profiles(c, kind))
            walk = c.execute(f"SELECT retry_at FROM {kind.backend}_profile_walk WHERE id=1").fetchone()
            return current, usable, walk["retry_at"] if walk else None
        current, usable, _ = await self.db.write(selected)
        if usable and await self._profile_ready(kind, current, files):
            return directory(current)
        walks = self.__dict__.setdefault("_profile_walk_locks", {})
        async with walks.setdefault(kind.backend, asyncio.Lock()):
            # Sessions that need another profile together wait for one walk, and take what it found.
            current, usable, retry_at = await self.db.write(selected)
            if usable and self.__dict__.get("_profiles_in_use", {}).get(kind.backend) == current:
                return directory(current)
            if retry_at is not None and retry_at > self.clock():
                return directory(current)  # All were found at their limit: the hold keeps new sessions waiting.
            return directory(await self._walk_profiles(kind, files, check) or current)

    async def _walk_profiles(self, kind: ProfileKind, files: dict[str, dict[str, Any]], check: Any) -> str | None:
        """Walk the rotation from the top of the order of preference, checking each profile whatever
        limit and reset time are recorded for it: an account is often refreshed before the time it
        named. The first the check finds usable becomes the selected one, its recorded limit cleared;
        each found at its limit records the reset the check reports. A check that cannot say lets a
        profile be used unless a limit is recorded for it. With none usable, the walk is tried again
        after WALK_RETRY_MS, and the hold keeps new sessions waiting until then; None."""
        def rotation(c):
            limits, excluded = self._profile_limits(c, kind, self.clock()), self._excluded_profiles(c, kind)
            return [(name, name in limits) for name in self._profile_priority(c, kind) if name not in excluded
                    and files.get(name, {}).get("signed_in", True) and not files.get(name, {}).get("problem")]
        config = self._profile_config(kind)
        checked = self.__dict__.setdefault("_profiles_checked", {})
        in_use = self.__dict__.setdefault("_profiles_in_use", {})
        for name, limited in await self.db.read(rotation):
            home = files.get(name, {}).get("home") or str(profile_home(kind, config, name))
            try:
                usable, until, windows = await check(name, home)
            except Exception as error:
                logger.warning("%s account check of %s did not answer: %s", kind.label, name, type(error).__name__)
                usable, until, windows = None, None, []
            if windows:
                await self.profile_usage_observed(kind, name, windows)
            if usable is False:
                await self.profile_limit_reached(kind, name, "The account check found it at its usage limit",
                                                 until=until, checked=True)
                checked[(kind.backend, name)] = (time.monotonic(), False)
                continue
            if usable is None and limited:
                continue

            def use(c, name=name):
                c.execute(f"DELETE FROM {kind.backend}_profile_limits WHERE profile=?", (name,))
                c.execute(f"DELETE FROM {kind.backend}_profile_walk")
                current = self._selected_profile(c, kind)
                if current != name:
                    self._select_profile(c, kind, name, "limit", current)
            await self.db.write(use)
            checked[(kind.backend, name)] = (time.monotonic(), True)
            in_use[kind.backend] = name
            return name

        def all_limited(c):
            now = self.clock()
            new = c.execute(f"SELECT 1 FROM {kind.backend}_profile_walk WHERE id=1").fetchone() is None
            c.execute(f"INSERT INTO {kind.backend}_profile_walk(id,all_limited_at,retry_at) VALUES(1,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET retry_at=excluded.retry_at", (now, now + WALK_RETRY_MS))
            if new:
                # Once until a profile can be used again, however often the walk is tried.
                self._event(c, f"{kind.backend}.profiles_limited", f"{kind.backend}-profiles",
                            {"until": now + WALK_RETRY_MS})
                self._record_profile_history(c, kind, "all_limited", None, until=now + WALK_RETRY_MS)
        await self.db.write(all_limited)
        in_use.pop(kind.backend, None)
        return None

    def _profile_check(self, kind: ProfileKind) -> Any:
        """The backend's account check: async (profile, home) -> ProfileCheck; None without one."""
        return None

    async def _profile_ready(self, kind: ProfileKind, name: str, files: dict[str, dict[str, Any]]) -> bool:
        """Whether conversations may use the profile: the one they used last, one an account
        check found usable a moment ago, or one the check finds usable now. A profile the check
        finds at its limit is recorded so; a check that cannot say lets it be used."""
        check = self._profile_check(kind)
        in_use = self.__dict__.setdefault("_profiles_in_use", {})
        if check is None or in_use.get(kind.backend) == name:
            return True
        locks = self.__dict__.setdefault("_profile_check_locks", {})
        async with locks.setdefault(kind.backend, asyncio.Lock()):
            # Sessions moving together wait for one check, and take what it found.
            checked = self.__dict__.setdefault("_profiles_checked", {})
            recent = checked.get((kind.backend, name))
            if in_use.get(kind.backend) == name:
                return True
            if recent is not None and time.monotonic() - recent[0] < CHECK_SECONDS:
                if recent[1]:
                    in_use[kind.backend] = name
                return recent[1]
            home = files.get(name, {}).get("home") or str(profile_home(kind, self._profile_config(kind), name))
            try:
                usable, until, windows = await check(name, home)
            except Exception as error:
                logger.warning("%s account check of %s did not answer: %s", kind.label, name, type(error).__name__)
                usable, until, windows = None, None, []
            if windows:
                await self.profile_usage_observed(kind, name, windows)
            checked[(kind.backend, name)] = (time.monotonic(), usable is not False)
            if usable is False:
                await self.profile_limit_reached(kind, name, "The account check found it at its usage limit",
                                                 until=until, checked=True)
                return False
            in_use[kind.backend] = name
            return True

    def _exhausted_window(self, c, kind: ProfileKind, profile: str, now: int) -> tuple[int, str] | None:
        """The last reset of the usage windows the backend last reported at their limit for a
        profile, and that window."""
        row = c.execute(f"SELECT limit_window,resets_at FROM {kind.backend}_profile_usage WHERE profile=? "
                        "AND resets_at>? AND (status='rejected' OR utilization>=1) ORDER BY resets_at DESC LIMIT 1",
                        (profile, now)).fetchone()
        return (row["resets_at"], row["limit_window"]) if row else None

    async def profile_limit_reached(self, kind: ProfileKind, profile: str, detail: str, *, worker_id=None,
                                    until: int | None = None, checked: bool = False) -> None:
        """Record that a profile reached a usage limit. `until`: when the limit resets, if known
        apart from the report (an account check). A report never shortens a recorded limit; an
        account check (`checked`) replaces it. A backend without a check moves on from the selected
        profile at once; one with a check does at its next session, by walking the rotation."""
        if profile not in profile_names(kind, self._profile_config(kind)):
            return
        files = self._profile_files_now(kind)
        known = until
        walks = self._profile_check(kind) is not None
        in_use = self.__dict__.get("_profiles_in_use", {})
        if in_use.get(kind.backend) == profile:
            in_use.pop(kind.backend)

        def record(c):
            now = self.clock()
            until, limit_type = (known, "unknown") if known is not None else kind.parse(str(detail), now)
            # What the report leaves out: when the window last reported at its limit resets (else
            # an hour from now), and which window that is, when it is named as a kind of limit.
            exhausted = self._exhausted_window(c, kind, profile, now) if until is None or limit_type == "unknown" else None
            if until is None:
                until = exhausted[0] if exhausted else now + UNKNOWN_RESET_MS
            if limit_type == "unknown" and exhausted and exhausted[1] in LIMIT_KINDS:
                limit_type = exhausted[1]
            until = max(until, now + 60_000)
            table = f"{kind.backend}_profile_limits"
            row = c.execute(f"SELECT limited_until FROM {table} WHERE profile=?", (profile,)).fetchone()
            if row is not None and row["limited_until"] > until and not checked:
                until = row["limited_until"]  # A later report never shortens a known limit.
            c.execute(f"INSERT INTO {table}(profile,limited_until,limit_type,detail,reported_at) "
                      "VALUES(?,?,?,?,?) ON CONFLICT(profile) DO UPDATE SET limited_until=excluded.limited_until,"
                      "limit_type=excluded.limit_type,detail=excluded.detail,reported_at=excluded.reported_at",
                      (profile, until, limit_type, str(detail)[:1000], now))
            self._event(c, f"{kind.backend}.profile_limited", f"{kind.backend}-profiles",
                        {"profile": profile, "limited_until": until, "limit_type": limit_type,
                         "detail": str(detail)[:1000]}, worker_id=worker_id)
            new = row is None or row["limited_until"] <= now
            if new:
                # Each limit once: other conversations on the profile report it too.
                self._record_profile_history(c, kind, "limited", profile, limit_type=limit_type, until=until)
            if (not walks and profile == self._selected_profile(c, kind)
                    and self._move_profile(c, kind, now, files, "limit") is None and new):
                # Once, when the last profile reaches its limit: in the Linux pilot each of 40
                # reviewers' reports of the same limit recorded it again.
                first = min(item["limited_until"] for item in self._profile_limits(c, kind, now).values())
                self._event(c, f"{kind.backend}.profiles_limited", f"{kind.backend}-profiles", {"until": first})
                self._record_profile_history(c, kind, "all_limited", None, until=first)
        await self.db.write(record)

    async def profile_usage_observed(self, kind: ProfileKind, profile: str, windows: list[dict[str, Any]]) -> None:
        """What the backend reported of a profile's usage windows: each window's state, the share
        used and when it resets."""
        if profile not in profile_names(kind, self._profile_config(kind)):
            return
        rows = [(profile, item["window"][:40], item["status"], item.get("utilization"), item.get("resets_at"))
                for item in windows
                if isinstance(item.get("window"), str) and item["window"] and item.get("status") in USAGE_STATUSES]
        if not rows:
            return

        def record(c):
            c.executemany(f"INSERT INTO {kind.backend}_profile_usage(profile,limit_window,status,utilization,resets_at,"
                          "observed_at) VALUES(?,?,?,?,?,?) ON CONFLICT(profile,limit_window) DO UPDATE SET "
                          "status=excluded.status,utilization=excluded.utilization,resets_at=excluded.resets_at,"
                          "observed_at=excluded.observed_at", [(*row, self.clock()) for row in rows])
        await self.db.write(record)

    def _profiles_hold(self, c, kind: ProfileKind) -> dict[str, Any] | None:
        """While every profile in the rotation is at its limit, the backend's new sessions wait:
        with an account check, until the walk is tried again (WALK_RETRY_MS after it found them
        all at their limit); without one, until the earliest recorded reset. The hold then ends by
        itself, without Resume. While none in the rotation can be used otherwise, after the
        operator took some out, they wait for the operator."""
        names = profile_names(kind, self._profile_config(kind))
        if not names:
            return None
        now, files = self.clock(), self._profile_files_now(kind)
        limits, excluded = self._profile_limits(c, kind, now), self._excluded_profiles(c, kind)
        rotation = [name for name in names if name not in excluded]
        label, walks = kind.label, self._profile_check(kind) is not None
        if walks:
            # The recorded limits do not hold sessions back: the walk checks each profile again.
            if any(files.get(name, {}).get("signed_in", True) and not files.get(name, {}).get("problem")
                   for name in rotation):
                walk = c.execute(f"SELECT all_limited_at,retry_at FROM {kind.backend}_profile_walk WHERE id=1").fetchone()
                if walk is None or walk["retry_at"] <= now:
                    return None
                moment = dt.datetime.fromtimestamp(walk["retry_at"] / 1000).strftime("%m-%d %H:%M")
                return {"code": f"{kind.backend}ProfilesLimited", "subject_id": None, "worker_id": None,
                        "created_at": walk["all_limited_at"], "until": walk["retry_at"],
                        "message": (f"Every {label} profile in the rotation was at its usage limit when checked. "
                                    f"They are checked again from {moment}, without Resume.")}
        elif any(self._profile_usable(name, limits, files, excluded) for name in rotation):
            return None
        limited = [] if walks else [limits[name] for name in rotation if name in limits]
        if limited:
            until = min(item["limited_until"] for item in limited)
            moment = dt.datetime.fromtimestamp(until / 1000).strftime("%m-%d %H:%M")
            return {"code": f"{kind.backend}ProfilesLimited", "subject_id": None, "worker_id": None,
                    "created_at": max(item["reported_at"] for item in limited), "until": until,
                    "message": (f"Every {label} profile in the rotation is at its usage limit. {label} sessions start "
                                f"again at {moment}, when the first one resets, without Resume.")}
        if not excluded:
            return None  # As before the rotation: an unusable profile's session reports what is wrong.
        message = (f"No {label} profile is in the rotation: tick one in the {label} tab." if not rotation else
                   f"No {label} profile in the rotation ({', '.join(rotation)}) can be used: sign in to one, "
                   f"or tick another in the {label} tab.")
        return {"code": f"{kind.backend}ProfilesUnavailable", "subject_id": None, "worker_id": None,
                "created_at": c.execute(f"SELECT MAX(excluded_at) FROM {kind.backend}_profile_excluded").fetchone()[0],
                "until": None, "message": message}

    def _profiles_view(self, c, kind: ProfileKind) -> dict[str, Any] | None:
        config = self._profile_config(kind)
        names = profile_names(kind, config)
        if not names:
            return None
        now, files, selected = self.clock(), self._profile_files_now(kind), self._selected_profile(c, kind)
        limits, excluded = self._profile_limits(c, kind, now), self._excluded_profiles(c, kind)
        last_limited = {row["profile"]: row["at"] for row in c.execute(
            f"SELECT profile,MAX(at) AS at FROM {kind.backend}_profile_history WHERE kind='limited' GROUP BY profile")}
        usage: dict[str, list[dict[str, Any]]] = {}
        # A window that has reset since it was reported says nothing about now.
        for row in c.execute(f"SELECT * FROM {kind.backend}_profile_usage WHERE resets_at IS NULL OR resets_at>? "
                             "ORDER BY profile,limit_window", (now,)):
            usage.setdefault(row["profile"], []).append(
                {"window": row["limit_window"], "status": row["status"], "utilization": row["utilization"],
                 "resets_at": row["resets_at"], "observed_at": row["observed_at"]})
        profiles = []
        # In the order of preference, which the operator may change.
        for position, name in enumerate(self._profile_priority(c, kind), 1):
            state, limit = files.get(name, {}), limits.get(name)
            profiles.append({"name": name, "home": state.get("home") or str(profile_home(kind, config, name)),
                             "priority": position, "first": name == names[0],
                             "selected": name == selected, "in_rotation": name not in excluded,
                             "signed_in": state.get("signed_in"), "problem": state.get("problem"),
                             "limited_until": limit["limited_until"] if limit else None,
                             "limit_type": limit["limit_type"] if limit else None,
                             "last_limited_at": last_limited.get(name), "usage": usage.get(name, [])})
        history = [dict(row) for row in c.execute(
            f"SELECT at,kind,profile,limit_type,until,previous,reason FROM {kind.backend}_profile_history "
            "ORDER BY id DESC LIMIT ?", (HISTORY_SHOWN,))]
        hold = self._profiles_hold(c, kind)
        return {"selected": selected, "held_until": hold["until"] if hold else None,
                "hold": {key: hold[key] for key in ("code", "message", "until")} if hold else None,
                "profiles": profiles, "history": history}

    async def profiles_report(self, kind: ProfileKind) -> dict[str, Any] | None:
        await self._fresh_profile_files(kind)
        return await self.db.read(lambda c: self._profiles_view(c, kind))

    async def profile_summary(self, kind: ProfileKind) -> dict[str, Any] | None:
        """The profile in use, for the dashboard's header; SQL only."""
        def read(c):
            view = self._profiles_view(c, kind)
            return {"selected": view["selected"], "held_until": view["held_until"],
                    "held": view["hold"]["code"] if view["hold"] else None, "count": len(view["profiles"])} if view else None
        return await self.db.read(read)

    def _set_profile_rotation(self, c, kind: ProfileKind, profile: str, included: bool) -> None:
        if included:
            changed = c.execute(f"DELETE FROM {kind.backend}_profile_excluded WHERE profile=?", (profile,)).rowcount
        else:
            changed = c.execute(f"INSERT OR IGNORE INTO {kind.backend}_profile_excluded(profile,excluded_at) VALUES(?,?)",
                                (profile, self.clock())).rowcount
        if changed:
            self._event(c, f"{kind.backend}.profile_rotation", f"{kind.backend}-profiles",
                        {"profile": profile, "in_rotation": included})
            self._record_profile_history(c, kind, "included" if included else "excluded", profile)

    def _profile_command(self, c, kind: ProfileKind, name: str, p: dict, actor) -> dict[str, Any] | None:
        self._role(actor, Role.OPERATOR)
        names = profile_names(kind, self._profile_config(kind))
        if name.endswith("_profile_priority"):
            # The order of preference: conversations use the first usable profile in it from their
            # next activation.
            order = p.get("order")
            if (not isinstance(order, list) or not all(isinstance(item, str) for item in order)
                    or len(set(order)) != len(order) or set(order) != set(names)):
                raise HubError("INVALID_INPUT", f"Expected every configured {kind.label} profile once, "
                               "in the order of preference", 422)
            self._set_profile_priority(c, kind, order)
            self._record_profile_history(c, kind, "priority", None, reason=",".join(order))
            self._move_profile(c, kind, self.clock(), self._profile_files_now(kind), "unusable")
            return self._profiles_view(c, kind)
        profile = p.get("profile")
        if profile not in names:
            raise HubError("INVALID_INPUT", f"Expected a configured {kind.label} profile", 422)
        if name.endswith("_profile_rotation"):
            # Taking the one in use out moves conversations to the next usable profile in the
            # rotation from their next activation; putting one in moves them there only when
            # none in the rotation could be used.
            if not isinstance(p.get("in_rotation"), bool):
                raise HubError("INVALID_INPUT", "Expected in_rotation: true or false", 422)
            self._set_profile_rotation(c, kind, profile, p["in_rotation"])
            if p["in_rotation"]:
                c.execute(f"DELETE FROM {kind.backend}_profile_walk")  # The walk tries it at the next session.
            selected = self._selected_profile(c, kind)
            self._move_profile(c, kind, self.clock(), self._profile_files_now(kind),
                               "excluded" if selected in self._excluded_profiles(c, kind) else "unusable")
            return self._profiles_view(c, kind)
        state = self._profile_files_now(kind).get(profile, {})
        if state.get("problem"):
            raise HubError("PROFILE_UNUSABLE", state["problem"], 409)
        if state.get("signed_in") is False:
            raise HubError("PROFILE_UNUSABLE", "Sign in first: " + kind.sign_in_command.format(home=state.get("home")), 409)
        # The operator knows the profile can be used again, as after an early reset or a new sign-in;
        # and selecting it puts it in the rotation, first in the order of preference. Its next use
        # is checked again.
        self.__dict__.get("_profiles_checked", {}).pop((kind.backend, profile), None)
        c.execute(f"DELETE FROM {kind.backend}_profile_limits WHERE profile=?", (profile,))
        c.execute(f"DELETE FROM {kind.backend}_profile_walk")
        self._set_profile_rotation(c, kind, profile, True)
        order = self._profile_priority(c, kind)
        if order[0] != profile:
            self._set_profile_priority(c, kind, [profile, *(item for item in order if item != profile)])
        self._select_profile(c, kind, profile, "operator", self._selected_profile(c, kind))
        return self._profiles_view(c, kind)

    def _account_profiles_hold(self, c, backend: str) -> dict[str, Any] | None:
        """The hold while a backend's profiles cannot be used (see _profiles_hold); None for a
        backend without profiles."""
        kind = PROFILE_KINDS.get(backend)
        return self._profiles_hold(c, kind) if kind is not None else None


# Filled by claude_profiles and codex_profiles, which define each backend's kind.
PROFILE_KINDS: dict[str, ProfileKind] = {}
