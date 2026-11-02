"""Claude subscription profiles used one at a time: a conversation is stored once whichever profile
runs it, a usage limit moves Claude to the next profile, and Claude waits only while every profile is
at its limit, until the first reset."""
from __future__ import annotations

import datetime as dt
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from formal_geyser.coordination.claude_profiles import (
    UNKNOWN_RESET_MS,
    parse_limit,
    prepare_profile,
    profile_files,
    profile_home,
    profile_homes,
)
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    ClaudeConfig,
    Command,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.native_permissions import protected_reads
from formal_geyser.coordination.runtime.claude_bridge import read_session, write_session
from formal_geyser.coordination.runtime.helpers import DecisionHelpers

NOW = 1_800_000_000_000
NAMES = ["main", "second", "third"]


def claude(tmp_path, names=NAMES) -> ClaudeConfig:
    return ClaudeConfig(home=str(tmp_path / "claude"), profiles=names)


def sign_in(config: ClaudeConfig, *names: str) -> None:
    for name in names:
        home = prepare_profile(config, name)
        (home / ".credentials.json").write_text("{}", encoding="utf-8")


async def command(hub, name, payload, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, payload=payload, key=uuid4().hex), Actor(role=role))


@pytest.fixture
async def hub(tmp_path):
    config = claude(tmp_path)
    initialize(tmp_path / "project", ProjectConfig(claude=config))
    sign_in(config, *NAMES)
    hub = Hub(tmp_path / "project")
    now = [NOW]
    hub.clock = lambda: now[0]
    hub.now = now
    await hub.refresh_claude_profiles()
    try:
        yield hub
    finally:
        await hub.close()


def test_profile_names_are_checked():
    for names in (["main", "main"], ["has space"], ["-dash-first"], ["x" * 33]):
        with pytest.raises(ValueError):
            ClaudeConfig(home="/h", profiles=names)
    assert ClaudeConfig(home="/h", profiles=["main", "alt.2", "c_3"]).profiles == ["main", "alt.2", "c_3"]


def test_a_conversation_is_stored_once_whichever_profile_runs_it(tmp_path):
    config = claude(tmp_path)
    files = profile_files(config)
    main, second = profile_home(config, "main"), profile_home(config, "second")
    assert main == tmp_path / "claude" and second == tmp_path / "claude" / "profiles" / "second"
    assert files["second"]["problem"] is None and files["second"]["signed_in"] is False
    for entry in ("projects", "task-based", "session-env", "sessions", "shell-snapshots"):
        assert (second / entry).is_symlink() and (second / entry).resolve() == (main / entry).resolve()
    # Started under one profile, continued under another: the same records, in one place.
    session = str(uuid4())
    write_session(main, {"session_id": session, "format": 1, "turns": ["first"]})
    record = read_session(second, session)
    write_session(second, {**record, "turns": [*record["turns"], "second"]})
    assert read_session(main, session)["turns"] == ["first", "second"]
    transcript = second / "projects" / "task-based" / (session + ".jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("{}\n", encoding="utf-8")
    assert (main / "projects" / "task-based" / (session + ".jsonl")).read_text(encoding="utf-8") == "{}\n"
    # Each profile keeps only its own sign-in.
    assert not (second / ".credentials.json").is_symlink()


def test_a_profile_with_conversations_of_its_own_is_refused(tmp_path):
    config = claude(tmp_path, ["main", "second"])
    second = profile_home(config, "second")
    (second / "projects" / "task-based").mkdir(parents=True)
    (second / "projects" / "task-based" / "old.jsonl").write_text("{}\n", encoding="utf-8")
    assert "keeps conversations of its own" in profile_files(config)["second"]["problem"]
    # An empty directory that Claude Code made in place of a link is replaced.
    (second / "projects" / "task-based" / "old.jsonl").unlink()
    (second / "projects" / "task-based").rmdir()
    assert profile_files(config)["second"]["problem"] is None
    assert (second / "projects").is_symlink()


def test_without_profiles_the_home_is_the_only_one(tmp_path):
    config = ClaudeConfig(home=str(tmp_path / "claude"))
    assert profile_homes(config) == [str(tmp_path / "claude")]
    assert profile_homes(ClaudeConfig()) == []


def test_reset_times_and_kinds_are_read_from_either_report():
    assert parse_limit("Claude subscription limit reached (five_hour); resets at 1800003600", NOW) == (
        1_800_003_600_000, "5h")
    assert parse_limit("Claude subscription limit reached (seven_day_opus); resets at 1800300000", NOW)[1] == "weekly"
    # The older message without its kind keeps working.
    assert parse_limit("Claude subscription limit reached; resets at 1800003600", NOW) == (1_800_003_600_000, "unknown")
    denver = ZoneInfo("America/Denver")
    now = dt.datetime(2026, 10, 25, 10, 0, tzinfo=denver)
    until, kind = parse_limit("Claude Code ended the turn: You've hit your session limit · resets 1:30pm (America/Denver)",
                              int(now.timestamp() * 1000))
    assert (dt.datetime.fromtimestamp(until / 1000, denver), kind) == (now.replace(hour=13, minute=30), "5h")
    # A time already past today is tomorrow's.
    until, _ = parse_limit("You've hit your session limit · resets 7am (America/Denver)", int(now.timestamp() * 1000))
    assert dt.datetime.fromtimestamp(until / 1000, denver) == now.replace(hour=7) + dt.timedelta(days=1)
    until, kind = parse_limit("You've hit your weekly limit · resets Oct 28, 9am (America/Denver)", int(now.timestamp() * 1000))
    assert (dt.datetime.fromtimestamp(until / 1000, denver), kind) == (now.replace(day=28, hour=9), "weekly")
    assert parse_limit("Limit reached", NOW) == (NOW + UNKNOWN_RESET_MS, "unknown")


async def test_a_limit_moves_claude_to_the_next_profile_and_back_only_when_needed(hub):
    async def chosen():
        return (await hub.claude_session_profile())["name"]

    async def hold():
        return await hub.db.read(lambda c: hub._dispatch_hold(c, "claude"))

    assert await chosen() == "main"
    await hub.claude_limit_reached("main", "Claude subscription limit reached (five_hour); resets at 1800003600")
    profile = await hub.claude_session_profile()
    assert profile == {"name": "second", "home": str(profile_home(hub.config.claude, "second"))}
    # Another conversation still running on the old profile reports the same limit: nothing moves.
    await hub.claude_limit_reached("main", "You've hit your session limit")
    assert await chosen() == "second" and await hold() is None
    await hub.claude_limit_reached("second", "Claude subscription limit reached (seven_day); resets at 1800500000")
    await hub.claude_limit_reached("third", "Claude subscription limit reached (five_hour); resets at 1800007200")
    held = await hold()
    assert (held["code"], held["until"], held["backend"]) == ("claudeProfilesLimited", 1_800_003_600_000, "claude")
    view = (await hub.backend_status())["claude_profiles"]
    assert view["held_until"] == 1_800_003_600_000 and view["selected"] == "third"
    assert [(item["name"], item["limit_type"]) for item in view["profiles"]] == [
        ("main", "5h"), ("second", "weekly"), ("third", "5h")]
    # The first reset ends the hold by itself, and the next session moves to the reset profile.
    hub.now[0] = 1_800_003_600_001
    assert await hold() is None
    assert await chosen() == "main"
    # It stays there after a profile earlier in the order resets: it moves only when limited.
    hub.now[0] = 1_800_007_200_001
    assert await chosen() == "main"
    assert (await hub.claude_profile_summary()) == {"selected": "main", "held_until": None, "held": None, "count": 3}


async def test_claude_stays_on_a_working_profile_when_one_ahead_resets(hub):
    async def chosen():
        return (await hub.claude_session_profile())["name"]

    assert await chosen() == "main"
    await hub.claude_limit_reached("main", "Claude subscription limit reached (five_hour); resets at 1800003600")
    assert await chosen() == "second"
    hub.now[0] = 1_800_003_600_001  # main has reset; second still works.
    assert await chosen() == "second"


async def test_profiles_that_cannot_be_used_are_passed_over(hub):
    (profile_home(hub.config.claude, "second") / ".credentials.json").unlink()
    await hub.refresh_claude_profiles()
    await hub.claude_limit_reached("main", "resets at 1800003600")
    assert (await hub.claude_session_profile())["name"] == "third"
    await hub.claude_limit_reached("third", "resets at 1800003600")
    # The profile without a sign-in does not count as available; the others are limited.
    assert (await hub.db.read(lambda c: hub._dispatch_hold(c, "claude")))["code"] == "claudeProfilesLimited"


async def test_the_operator_selects_a_profile_and_clears_its_limit(hub):
    await hub.claude_limit_reached("main", "resets at 1800003600")
    view = await command(hub, "runtime.select_claude_profile", {"profile": "main"})
    assert view["selected"] == "main" and view["profiles"][0]["limited_until"] is None
    assert (await hub.claude_session_profile())["name"] == "main"
    (profile_home(hub.config.claude, "third") / ".credentials.json").unlink()
    await hub.refresh_claude_profiles()
    for payload, code in (({"profile": "fourth"}, "INVALID_INPUT"), ({"profile": "third"}, "PROFILE_UNUSABLE")):
        with pytest.raises(HubError) as error:
            await command(hub, "runtime.select_claude_profile", payload)
        assert error.value.code == code
    with pytest.raises(HubError):
        await command(hub, "runtime.select_claude_profile", {"profile": "second"}, role=Role.RUNNER)


async def test_a_profile_out_of_the_rotation_is_never_switched_to(hub):
    async def chosen():
        return (await hub.claude_session_profile())["name"]

    async def hold():
        return await hub.db.read(lambda c: hub._dispatch_hold(c, "claude"))

    def rotation(name, included):
        return command(hub, "runtime.set_claude_profile_rotation", {"profile": name, "in_rotation": included})

    # Taking the one in use out moves Claude to the next one in the rotation at once.
    view = await rotation("main", False)
    assert view["selected"] == "second" and [item["in_rotation"] for item in view["profiles"]] == [False, True, True]
    assert await chosen() == "second"
    # Limits pass over it too, and Claude waits for the first reset in the rotation.
    await hub.claude_limit_reached("second", "resets at 1800003600")
    assert await chosen() == "third"
    await hub.claude_limit_reached("third", "resets at 1800007200")
    held = await hold()
    assert (held["code"], held["until"]) == ("claudeProfilesLimited", 1_800_003_600_000)
    hub.now[0] = 1_800_003_600_001
    assert await hold() is None and await chosen() == "second"
    # With none in the rotation, Claude waits for the operator, not for a reset.
    await rotation("second", False)
    await rotation("third", False)
    held = await hold()
    assert (held["code"], held["until"]) == ("claudeProfilesUnavailable", None)
    assert (await hub.claude_profile_summary())["held"] == "claudeProfilesUnavailable"
    # Putting one back in moves Claude there; selecting one by hand puts it back in.
    hub.now[0] = 1_800_007_200_001
    view = await rotation("third", True)
    assert view["selected"] == "third" and view["hold"] is None
    view = await command(hub, "runtime.select_claude_profile", {"profile": "main"})
    assert view["selected"] == "main" and view["profiles"][0]["in_rotation"] is True
    history = [(item["kind"], item["profile"], item["reason"]) for item in reversed(view["history"])]
    assert history[:2] == [("excluded", "main", None), ("selected", "second", "excluded")]
    assert history[-4:] == [("included", "third", None), ("selected", "third", "excluded"), ("included", "main", None),
                            ("selected", "main", "operator")]
    for payload in ({"profile": "fourth", "in_rotation": False}, {"profile": "main", "in_rotation": "no"}):
        with pytest.raises(HubError) as error:
            await command(hub, "runtime.set_claude_profile_rotation", payload)
        assert error.value.code == "INVALID_INPUT"


class FakeHub:
    def __init__(self):
        self.calls = []

    async def hold_dispatch(self, code, detail, *, subject_id, worker_id=None):
        self.calls.append(("hold", code))
        return True

    async def claude_limit_reached(self, profile, detail, *, subject_id=None, worker_id=None):
        self.calls.append(("limit", profile))
        if profile == "broken":
            raise RuntimeError("database unavailable")


class Runner(DecisionHelpers):
    def __init__(self):
        self.hub = FakeHub()


class Harness:
    def __init__(self, profile):
        self.claude_profile = profile


async def test_a_claude_usage_limit_is_recorded_for_its_profile_not_held():
    runner = Runner()
    limit = {"code": "claudeUsageLimit", "message": "Claude subscription limit reached; resets at 1800003600"}
    await runner._note_account_hold(limit, subject_id="a", worker_id="w", harness=Harness("main"))
    assert runner.hub.calls == [("limit", "main")]
    # Unrecorded, or from an activation without a profile, it holds Claude as before.
    await runner._note_account_hold(limit, subject_id="a", worker_id="w", harness=Harness("broken"))
    await runner._note_account_hold(limit, subject_id="a", worker_id="w", harness=Harness(None))
    # Other account errors hold as before.
    await runner._note_account_hold({"code": "claudeSignInRequired", "message": "x"}, subject_id="a",
                                    worker_id="w", harness=Harness("main"))
    assert runner.hub.calls[1:] == [("limit", "broken"), ("hold", "claudeUsageLimit"), ("hold", "claudeUsageLimit"),
                                    ("hold", "claudeSignInRequired")]


def test_every_profiles_sign_in_is_protected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing-runtime"))
    config = claude(tmp_path)
    sign_in(config, *NAMES)
    protected = protected_reads(str(tmp_path / "codex"), profile_homes(config))
    assert {str((profile_home(config, name) / ".credentials.json").resolve()) for name in NAMES} <= set(protected)


async def test_the_claude_tab_keeps_each_limit_and_switch_once(hub):
    await hub.claude_limit_reached("main", "Claude subscription limit reached (five_hour); resets at 1800003600")
    await hub.claude_limit_reached("main", "You've hit your session limit")  # The same limit, reported again.
    await hub.claude_limit_reached("second", "Claude subscription limit reached (seven_day); resets at 1800500000")
    await hub.claude_limit_reached("third", "resets at 1800007200")
    view = await hub.claude_profiles()
    assert [(item["kind"], item["profile"], item["reason"]) for item in reversed(view["history"])] == [
        ("limited", "main", None), ("selected", "second", "limit"), ("limited", "second", None),
        ("selected", "third", "limit"), ("limited", "third", None), ("all_limited", None, None)]
    assert view["history"][0]["until"] == 1_800_003_600_000
    assert [item["last_limited_at"] for item in view["profiles"]] == [NOW, NOW, NOW]
    await command(hub, "runtime.select_claude_profile", {"profile": "main"})
    latest = (await hub.claude_profiles())["history"][0]
    assert (latest["kind"], latest["profile"], latest["previous"], latest["reason"]) == ("selected", "main", "third", "operator")


async def test_reported_usage_is_shown_until_its_window_resets(hub):
    report = {"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.82, "resetsAt": 1800003600}
    await hub.claude_usage_observed("main", report)
    await hub.claude_usage_observed("main", {**report, "rateLimitType": "seven_day", "utilization": 0.4,
                                             "resetsAt": 1800500000, "status": "allowed"})
    for ignored in ({**report, "status": "unknown"}, {**report, "rateLimitType": None}):
        await hub.claude_usage_observed("main", ignored)
    await hub.claude_usage_observed("fourth", report)  # Not a configured profile.
    usage = (await hub.claude_profiles())["profiles"][0]["usage"]
    assert [(item["window"], item["status"], item["utilization"], item["resets_at"]) for item in usage] == [
        ("five_hour", "allowed_warning", 0.82, 1_800_003_600_000), ("seven_day", "allowed", 0.4, 1_800_500_000_000)]
    hub.now[0] = 1_800_003_600_001  # The 5-hour window has reset: what was reported for it no longer applies.
    assert [item["window"] for item in (await hub.claude_profiles())["profiles"][0]["usage"]] == ["seven_day"]


async def test_a_claude_activation_passes_usage_reports_to_its_handler(tmp_path):
    from types import SimpleNamespace

    from formal_geyser.coordination.runtime.claude import ClaudeHarness

    harness, seen = ClaudeHarness(claude(tmp_path)), []

    async def handler(info):
        seen.append(info["status"])
    harness.set_rate_limit_handler(handler)
    harness.session = SimpleNamespace(thread_id="thread")
    await harness._notification("account/rateLimit", {"threadId": "thread", "status": "allowed_warning"})
    await harness._notification("account/rateLimit", {"threadId": "other", "status": "rejected"})
    assert seen == ["allowed_warning"]
