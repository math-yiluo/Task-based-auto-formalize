"""Codex accounts used one at a time, as Claude's profiles: a thread is stored once whichever profile
runs it, a usage limit moves Codex to the next profile, and Codex waits only while every profile is
at its limit, until the first reset. Without configured profiles nothing changes."""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from formal_geyser.coordination.codex_profiles import (
    _codex_reset,
    account_check_result,
    codex_usage,
    prepare_profile,
    profile_files,
    profile_home,
    profile_homes,
    read_account_limits,
    sign_in_homes,
)
from formal_geyser.coordination.config import codex_profiles_equal, initialize
from formal_geyser.coordination.errors import HubError
from formal_geyser.coordination.hub import Hub
from formal_geyser.coordination.models import (
    Actor,
    ClaudeConfig,
    CodexConfig,
    Command,
    ProjectConfig,
    Role,
)
from formal_geyser.coordination.native_permissions import protected_reads
from formal_geyser.coordination.runtime.codex import CodexHarness
from formal_geyser.coordination.runtime.helpers import DecisionHelpers

NAMES = ["main", "second", "third", "fourth"]
LIMIT = ("You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits "
         "or try again at {when}.")


def local_ms(*fields: int) -> int:
    """A moment in this machine's local time, as Codex names it."""
    return int(dt.datetime(*fields).timestamp() * 1000)


# When the pilot's last Codex account reached its weekly limit.
NOW = local_ms(2026, 10, 27, 7, 45)


def codex(tmp_path, names=NAMES) -> CodexConfig:
    return CodexConfig(home=str(tmp_path / "codex"), profiles=names)


def sign_in(config: CodexConfig, *names: str) -> None:
    for name in names:
        (prepare_profile(config, name) / "auth.json").write_text("{}", encoding="utf-8")


async def command(hub, name, payload, role=Role.OPERATOR):
    return await hub.execute(Command(name=name, payload=payload, key=uuid4().hex), Actor(role=role))


@pytest.fixture
async def hub(tmp_path):
    config = codex(tmp_path)
    initialize(tmp_path / "project", ProjectConfig(codex=config))
    sign_in(config, *NAMES)
    hub = Hub(tmp_path / "project")
    now = [NOW]
    hub.clock = lambda: now[0]
    hub.now = now
    # The account check reads each profile's usage from Codex; here it cannot say, as when Codex
    # does not answer, unless a test gives each home its reply.
    hub.limits_replies, hub.limits_reads = {}, []

    async def reader(home):
        hub.limits_reads.append(home.rsplit("/", 1)[-1])
        return hub.limits_replies.get(home.rsplit("/", 1)[-1])
    hub.codex_limits_reader = reader
    await hub.refresh_codex_profiles()
    try:
        yield hub
    finally:
        await hub.close()


def test_profile_names_are_checked_and_without_them_the_home_is_used_alone(tmp_path):
    for names in (["main", "main"], ["has space"], ["-dash"], ["x" * 33]):
        with pytest.raises(ValueError):
            CodexConfig(home="/h", profiles=names)
    plain = CodexConfig(home=str(tmp_path / "codex"))
    # Unlike Claude's, a Codex home without profiles has no rotation: a limit holds Codex until Resume.
    assert profile_homes(plain) == [] and sign_in_homes(plain) == [str(tmp_path / "codex")]
    assert sign_in_homes(CodexConfig()) == []
    assert sign_in_homes(codex(tmp_path)) == profile_homes(codex(tmp_path))


def test_a_thread_is_stored_once_whichever_profile_runs_it(tmp_path):
    config = codex(tmp_path)
    files = profile_files(config)
    main, second = profile_home(config, "main"), profile_home(config, "second")
    assert main == tmp_path / "codex" and second == tmp_path / "codex" / "profiles" / "second"
    assert files["second"]["problem"] is None and files["second"]["signed_in"] is False
    for entry in ("sessions", "archived_sessions", "shell_snapshots", "thread-writer-locks"):
        assert (second / entry).is_symlink() and (second / entry).resolve() == (main / entry).resolve()
    rollout = second / "sessions" / "2026" / "10" / "27" / "rollout-thread.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n", encoding="utf-8")
    assert (main / "sessions" / "2026" / "10" / "27" / "rollout-thread.jsonl").read_text(encoding="utf-8") == "{}\n"
    # Each profile keeps its own sign-in and its own list of trusted projects.
    sign_in(config, "second")
    assert profile_files(config)["second"]["signed_in"] is True and not (second / "auth.json").is_symlink()
    assert not (second / "config.toml").exists()


def test_every_profiles_sign_in_is_protected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing-runtime"))
    config = codex(tmp_path)
    sign_in(config, *NAMES)
    protected = protected_reads(sign_in_homes(config), [str(tmp_path / "claude")])
    assert {str((profile_home(config, name) / "auth.json").resolve()) for name in NAMES} <= set(protected)
    # A sign-in made later is protected from the next reading (each activation, each Lean check).
    later = codex(tmp_path, [*NAMES, "fifth"])
    assert str((profile_home(later, "fifth") / "auth.json").resolve()) not in protected_reads(sign_in_homes(later), None)
    sign_in(later, "fifth")
    assert str((profile_home(later, "fifth") / "auth.json").resolve()) in protected_reads(sign_in_homes(later), None)


def test_the_reset_codex_names_is_read_in_local_time_to_the_end_of_its_minute():
    now = local_ms(2026, 10, 27, 7, 45)
    assert _codex_reset(LIMIT.format(when="Nov 1st, 2026 11:09 PM"), now) == (local_ms(2026, 11, 1, 23, 10), "unknown")
    assert _codex_reset(LIMIT.format(when="Nov 22nd, 2026 9:05 AM"), now)[0] == local_ms(2026, 11, 22, 9, 6)
    # The same day names only the time; a time already past is tomorrow's.
    assert _codex_reset(LIMIT.format(when="11:09 PM"), now)[0] == local_ms(2026, 10, 27, 23, 10)
    assert _codex_reset(LIMIT.format(when="6:30 AM"), now)[0] == local_ms(2026, 10, 28, 6, 31)
    assert _codex_reset("You've hit your usage limit. Try again later.", now) == (None, "unknown")
    assert _codex_reset(LIMIT.format(when="Feb 30th, 2026 1:00 AM"), now) == (None, "unknown")


def test_codex_usage_reports_name_the_5_hour_and_weekly_windows():
    snapshot = {"limitId": "codex", "primary": {"usedPercent": 41, "windowDurationMins": 300, "resetsAt": 1800003600},
                "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 1800500000}}
    assert codex_usage(snapshot) == [
        {"window": "5h", "status": "allowed", "utilization": 0.41, "resets_at": 1_800_003_600_000},
        {"window": "weekly", "status": "rejected", "utilization": 1.0, "resets_at": 1_800_500_000_000}]
    other = codex_usage({"limitId": "codex_model", "primary": {"usedPercent": 5, "windowDurationMins": 1440}})
    assert other == [{"window": "codex_model 1d", "status": "allowed", "utilization": 0.05, "resets_at": None}]
    assert codex_usage({"primary": None}) == [] and codex_usage("broken") == []


async def test_a_limit_moves_codex_to_the_next_profile_and_codex_waits_while_all_are_limited(hub):
    async def chosen():
        return (await hub.codex_session_profile())["name"]

    async def hold(backend="codex"):
        return await hub.db.read(lambda c: hub._dispatch_hold(c, backend))

    assert await chosen() == "main"
    await hub.codex_limit_reached("main", LIMIT.format(when="Nov 1st, 2026 11:09 PM"))
    profile = await hub.codex_session_profile()
    assert profile == {"name": "second", "home": str(profile_home(hub.config.codex, "second"))}
    # Another conversation still running on the old profile reports the same limit: nothing moves.
    await hub.codex_limit_reached("main", LIMIT.format(when="Nov 1st, 2026 11:09 PM"))
    assert await chosen() == "second" and await hold() is None
    await hub.codex_limit_reached("second", "You've hit your usage limit. Try again later.")
    await hub.codex_limit_reached("third", LIMIT.format(when="Nov 2nd, 2026 12:56 AM"))
    assert await chosen() == "fourth"
    await hub.codex_limit_reached("fourth", LIMIT.format(when="Nov 3rd, 2026 1:00 AM"))
    # The check cannot say here, so the walk passes over each profile with a recorded limit: it
    # finds none, and new sessions wait 30 minutes before it is tried again.
    assert await chosen() == "fourth"
    held = await hold()
    assert (held["code"], held["until"], held["backend"]) == ("codexProfilesLimited", NOW + 1_800_000, "codex")
    assert await hold("claude") is None  # Claude is another account.
    view = (await hub.backend_status())["codex_profiles"]
    assert view["selected"] == "fourth" and view["held_until"] == NOW + 1_800_000
    summary = await hub.codex_profile_summary()
    assert summary == {"selected": "fourth", "held_until": NOW + 1_800_000, "held": "codexProfilesLimited", "count": 4}
    # second's limit (an hour after its report, which named no time) has passed: the hold has ended
    # and the next walk finds second.
    hub.now[0] = NOW + 3_600_001
    assert await hold() is None and await chosen() == "second"


async def test_a_limit_without_a_reset_time_uses_the_window_reported_at_its_limit(hub):
    await hub.codex_usage_observed("main", {"limitId": "codex",
                                            "primary": {"usedPercent": 60, "windowDurationMins": 300, "resetsAt": 1800003600},
                                            "secondary": {"usedPercent": 100, "windowDurationMins": 10080,
                                                          "resetsAt": 1800500000}})
    await hub.codex_limit_reached("main", "You've hit your usage limit. Try again later.")
    assert (await hub.codex_session_profile())["name"] == "second"
    view = await hub.codex_profiles()
    main = view["profiles"][0]
    assert (main["limited_until"], main["limit_type"]) == (1_800_500_000_000, "weekly")
    assert [(item["window"], item["utilization"]) for item in main["usage"]] == [("5h", 0.6), ("weekly", 1.0)]
    assert [(item["kind"], item["profile"]) for item in reversed(view["history"])] == [
        ("limited", "main"), ("selected", "second")]
    await hub.codex_usage_observed("fifth", {"primary": {"usedPercent": 1}})  # Not a configured profile.


async def test_the_operator_selects_profiles_and_takes_them_out_of_the_rotation(hub):
    await hub.codex_limit_reached("main", LIMIT.format(when="Nov 1st, 2026 11:09 PM"))
    view = await command(hub, "runtime.select_codex_profile", {"profile": "main"})
    assert view["selected"] == "main" and view["profiles"][0]["limited_until"] is None
    view = await command(hub, "runtime.set_codex_profile_rotation", {"profile": "main", "in_rotation": False})
    assert view["selected"] == "second" and [item["in_rotation"] for item in view["profiles"]] == [False, True, True, True]
    for name in ("second", "third", "fourth"):
        await command(hub, "runtime.set_codex_profile_rotation", {"profile": name, "in_rotation": False})
    held = await hub.db.read(lambda c: hub._dispatch_hold(c, "codex"))
    assert (held["code"], held["until"]) == ("codexProfilesUnavailable", None)
    assert "Codex tab" in held["message"]
    (profile_home(hub.config.codex, "third") / "auth.json").unlink()
    await hub.refresh_codex_profiles()
    for payload, code in (({"profile": "fifth"}, "INVALID_INPUT"), ({"profile": "third"}, "PROFILE_UNUSABLE")):
        with pytest.raises(HubError) as error:
            await command(hub, "runtime.select_codex_profile", payload)
        assert error.value.code == code
    with pytest.raises(HubError) as error:
        await command(hub, "runtime.select_codex_profile", {"profile": "third"})
    assert "codex login" in error.value.message
    with pytest.raises(HubError):
        await command(hub, "runtime.select_codex_profile", {"profile": "second"}, role=Role.RUNNER)


async def test_the_operators_priority_decides_the_first_profile_and_each_switch(hub):
    async def chosen():
        return (await hub.codex_session_profile())["name"]

    def priority(order):
        return command(hub, "runtime.set_codex_profile_priority", {"order": order})

    view = await hub.codex_profiles()
    assert [(item["name"], item["priority"], item["first"]) for item in view["profiles"]] == [
        ("main", 1, True), ("second", 2, False), ("third", 3, False), ("fourth", 4, False)]
    # Before any profile is in use, a new order picks the first one at once.
    view = await priority(["third", "main", "second", "fourth"])
    assert view["selected"] == "third" and [item["name"] for item in view["profiles"]] == ["third", "main", "second", "fourth"]
    assert await chosen() == "third"
    # At its limit, the walk from the top of the order finds main; once third can be used again,
    # conversations stay on main, which works.
    await hub.codex_limit_reached("third", LIMIT.format(when="2:00 PM"))
    assert await chosen() == "main"
    hub.now[0] = local_ms(2026, 10, 27, 14, 1) + 1
    assert await chosen() == "main"
    history = [(item["kind"], item["profile"], item["reason"]) for item in reversed((await hub.codex_profiles())["history"])]
    assert history == [("priority", None, "third,main,second,fourth"), ("selected", "third", "priority"),
                       ("limited", "third", None), ("selected", "main", "limit")]
    # Using a profile by hand puts it first.
    view = await command(hub, "runtime.select_codex_profile", {"profile": "fourth"})
    assert view["selected"] == "fourth"
    assert [item["name"] for item in view["profiles"]] == ["fourth", "third", "main", "second"]
    for order in (["main", "second", "third"], ["main", "main", "second", "third"],
                  ["main", "second", "third", "fifth"], "main", None):
        with pytest.raises(HubError) as error:
            await priority(order)
        assert error.value.code == "INVALID_INPUT"


async def test_without_codex_profiles_a_usage_limit_holds_codex_until_resume(tmp_path):
    initialize(tmp_path / "project", ProjectConfig(codex=CodexConfig(home=str(tmp_path / "codex"))))
    hub = Hub(tmp_path / "project")
    try:
        assert await hub.codex_session_profile() is None and await hub.codex_profiles() is None
        assert (await hub.backend_status())["codex_profiles"] is None
    finally:
        await hub.close()


class FakeHub:
    def __init__(self):
        self.calls = []

    async def hold_dispatch(self, code, detail, *, subject_id, worker_id=None):
        self.calls.append(("hold", code))
        return True

    async def codex_limit_reached(self, profile, detail, *, subject_id=None, worker_id=None):
        self.calls.append(("codex", profile))

    async def claude_limit_reached(self, profile, detail, *, subject_id=None, worker_id=None):
        self.calls.append(("claude", profile))


class Runner(DecisionHelpers):
    def __init__(self):
        self.hub = FakeHub()


async def test_a_codex_usage_limit_is_recorded_for_its_profile_not_held():
    runner = Runner()
    limit = {"code": "usageLimitExceeded", "message": LIMIT.format(when="Nov 1st, 2026 11:09 PM")}
    await runner._note_account_hold(limit, subject_id="a", worker_id="w", harness=SimpleNamespace(codex_profile="main"))
    # Without a profile (none configured) it holds Codex as before; a sign-in error always holds.
    await runner._note_account_hold(limit, subject_id="a", worker_id="w", harness=SimpleNamespace(codex_profile=None))
    await runner._note_account_hold({"code": "unauthorized", "message": "x"}, subject_id="a", worker_id="w",
                                    harness=SimpleNamespace(codex_profile="main", signed_in_again=lambda: False))
    # A Claude limit still goes to Claude's profile.
    await runner._note_account_hold({"code": "claudeUsageLimit", "message": "x"}, subject_id="a", worker_id="w",
                                    harness=SimpleNamespace(claude_profile="second"))
    assert runner.hub.calls == [("codex", "main"), ("hold", "usageLimitExceeded"), ("hold", "unauthorized"),
                                ("claude", "second")]


async def test_a_codex_activation_signs_in_with_its_profile_and_shares_the_databases(tmp_path):
    config = codex(tmp_path)
    home = prepare_profile(config, "third")
    harness = CodexHarness(config, command=["codex"], account={"name": "third", "home": str(home)})
    environment = harness.process_environment()
    assert environment["CODEX_HOME"] == str(home.resolve())
    assert environment["CODEX_SQLITE_HOME"] == str((tmp_path / "codex").resolve())
    assert harness.codex_home() == home.resolve() and harness.codex_profile == "third"
    # The launch command keeps the shared databases too.
    executable = tmp_path / "codex-binary"
    executable.write_text("", encoding="utf-8")
    launched = CodexHarness.launch_command(config.model_copy(update={"executable": str(executable)}))
    assert launched[launched.index("-c") + 1] == "sqlite_home=" + f'"{(tmp_path / "codex").resolve()}"'
    plain = CodexHarness(config, command=["codex"])
    assert plain.process_environment()["CODEX_HOME"] == str((tmp_path / "codex").resolve()) and plain.codex_profile is None


async def test_a_codex_activation_passes_usage_reports_to_its_handler(tmp_path):
    harness, seen = CodexHarness(codex(tmp_path), command=["codex"]), []

    async def handler(snapshot):
        seen.append(snapshot["primary"]["usedPercent"])
    harness.set_rate_limit_handler(handler)
    # The account's report names no thread, and arrives before any session too.
    await harness._notification("account/rateLimits/updated", {"rateLimits": {"primary": {"usedPercent": 12}}})

    async def broken(snapshot):
        raise RuntimeError("database unavailable")
    harness.set_rate_limit_handler(broken)
    await harness._notification("account/rateLimits/updated", {"rateLimits": {}})  # Observation never fails a turn.
    assert seen == [12]


def test_moving_between_profiles_keeps_a_conversations_binding(tmp_path):
    # A saved binding records the backend's configuration; adding a profile must not invalidate it.
    before = {**CodexConfig(home="/h").model_dump(mode="json")}
    after = {**CodexConfig(home="/h", profiles=["main", "second"]).model_dump(mode="json")}
    assert codex_profiles_equal(before, after)
    claude = ClaudeConfig(home="/c", profiles=["main", "second", "third"]).model_dump(mode="json")
    assert codex_profiles_equal(claude, {**claude, "profiles": [*claude["profiles"], "fourth"]})
    assert not codex_profiles_equal(before, {**before, "model": "other"})


# What account/rateLimits/read answered in the pilot for the Utah account at its weekly limit.
AT_LIMIT = {"ordinaryUsageAllowed": False, "accountId": "a", "rateLimits": {
    "limitId": "codex", "primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 1793599750},
    "secondary": None, "planType": "promax", "rateLimitReachedType": "rate_limit_reached"}}
WITHIN = {"ordinaryUsageAllowed": True, "rateLimits": {
    "limitId": "codex", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1793140000},
    "secondary": {"usedPercent": 36, "windowDurationMins": 10080, "resetsAt": 1793600000}}}


def test_an_account_check_reads_codexs_own_answer_else_the_windows():
    usable, until, windows = account_check_result(AT_LIMIT)
    assert (usable, until) == (False, 1_793_599_750_000) and windows[0]["window"] == "weekly"
    assert account_check_result(WITHIN)[:2] == (True, None)
    # Without Codex's answer, a window at 100% decides; without windows, nothing is known.
    unknown = {**AT_LIMIT, "ordinaryUsageAllowed": None}
    assert account_check_result(unknown)[:2] == (False, 1_793_599_750_000)
    assert account_check_result({"rateLimits": {}}) == (None, None, [])
    assert account_check_result(None) == (None, None, [])


FAKE_CODEX = '''#!{python}
import json, os, sys, time
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({{"method": "remoteControl/status/changed", "params": {{}}}}), flush=True)
        print(json.dumps({{"id": message["id"], "result": {{"userAgent": "fake/0.160.0"}}}}), flush=True)
    elif message.get("method") == "account/rateLimits/read":
        if os.environ["CODEX_HOME"].endswith("silent"):
            time.sleep(60)
        print(json.dumps({{"id": message["id"], "result": {{"ordinaryUsageAllowed": True,
            "home": os.environ["CODEX_HOME"], "storage": os.environ["CODEX_SQLITE_HOME"], "args": sys.argv[1:]}}}}), flush=True)
'''


async def test_an_account_check_asks_a_short_codex_signed_in_from_the_profile(tmp_path):
    fake = tmp_path / "codex"
    fake.write_text(FAKE_CODEX.format(python=sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    reply = await read_account_limits(str(fake), str(tmp_path / "profiles" / "nus"), str(tmp_path))
    assert reply == {"ordinaryUsageAllowed": True, "home": str(tmp_path / "profiles" / "nus"), "storage": str(tmp_path),
                     "args": ["-c", f'sqlite_home="{tmp_path}"', "app-server"]}
    with pytest.raises(asyncio.TimeoutError):
        await read_account_limits(str(fake), str(tmp_path / "silent"), str(tmp_path), timeout=1.0)


async def test_conversations_move_only_to_an_account_the_check_finds_usable(hub):
    async def chosen():
        return (await hub.codex_session_profile())["name"]

    # The first activation checks the profile in use; later ones on it are not checked again.
    assert await chosen() == "main" and await chosen() == "main" and hub.limits_reads == ["codex"]
    # main reaches its limit. The walk from the top checks main again (it cannot say, and main's
    # limit is recorded), then second, at its weekly limit too: the check records until when; then
    # third, which it finds usable. Sessions that need a profile together wait for that one walk.
    hub.limits_replies.update(second=AT_LIMIT, third=WITHIN)
    await hub.codex_limit_reached("main", LIMIT.format(when="Nov 1st, 2026 11:09 PM"))
    names = await asyncio.gather(*(chosen() for _ in range(10)))
    assert set(names) == {"third"} and hub.limits_reads == ["codex", "codex", "second", "third"]
    view = await hub.codex_profiles()
    second = next(item for item in view["profiles"] if item["name"] == "second")
    assert (second["limited_until"], second["limit_type"]) == (1_793_599_750_000, "weekly")
    assert [(item["kind"], item["profile"]) for item in reversed(view["history"])] == [
        ("limited", "main"), ("limited", "second"), ("selected", "third")]
    # A check that cannot answer lets the move happen: the session reports a limit if there is one.
    hub.limits_replies.clear()
    await hub.codex_limit_reached("third", LIMIT.format(when="Nov 2nd, 2026 1:00 AM"))
    assert await chosen() == "fourth"

    async def broken(home):
        raise ConnectionError("no answer")
    hub.codex_limits_reader = broken
    await command(hub, "runtime.select_codex_profile", {"profile": "third"})
    assert await chosen() == "third"


async def test_every_profile_at_its_limit_is_recorded_once(hub):
    for name in NAMES:
        for _ in range(3):  # Each running conversation reports its profile's limit.
            await hub.codex_limit_reached(name, LIMIT.format(when="Nov 2nd, 2026 1:00 AM"))
    for _ in range(3):  # Each walk finds them all at their limit; it is tried again after 30 minutes.
        await hub.codex_session_profile()
        hub.now[0] += 1_800_001
    history = (await hub.codex_profiles())["history"]
    assert [item["kind"] for item in history].count("all_limited") == 1
    assert [item["kind"] for item in history].count("limited") == 4


async def test_conversations_stay_on_a_working_account_when_one_ahead_can_be_used_again(hub):
    async def chosen():
        return (await hub.codex_session_profile())["name"]

    assert await chosen() == "main"
    await hub.codex_limit_reached("main", LIMIT.format(when="9:00 AM"))
    assert await chosen() == "second"
    # main's limit passes and its account has quota again; second still works, so nothing moves
    # and nothing is checked.
    reads = list(hub.limits_reads)
    hub.now[0] = local_ms(2026, 10, 27, 9, 2)
    hub.limits_replies["codex"] = WITHIN
    assert [await chosen() for _ in range(3)] == ["second"] * 3 and hub.limits_reads == reads


async def test_a_switch_checks_each_account_from_the_top_whatever_reset_it_recorded(hub):
    async def chosen():
        return (await hub.codex_session_profile())["name"]

    assert await chosen() == "main"
    await hub.codex_limit_reached("main", LIMIT.format(when="Nov 1st, 2026 11:09 PM"))
    assert await chosen() == "second"
    # Codex refreshed main's account days before the time it named. When second reaches its limit,
    # the walk checks main first, finds it usable and clears its recorded limit.
    hub.limits_replies["codex"] = WITHIN
    await hub.codex_limit_reached("second", LIMIT.format(when="Nov 2nd, 2026 1:00 AM"))
    assert await chosen() == "main"
    assert (await hub.codex_profiles())["profiles"][0]["limited_until"] is None
    # A check that finds a profile at its limit records the reset it reports, earlier than the
    # recorded one too; the walk goes on to the next.
    await hub.codex_limit_reached("third", LIMIT.format(when="Nov 3rd, 2026 8:02 PM"))
    hub.limits_replies.update(codex=AT_LIMIT, second=AT_LIMIT, third=AT_LIMIT, fourth=WITHIN)
    await hub.codex_limit_reached("main", LIMIT.format(when="Nov 1st, 2026 11:09 PM"))
    assert await chosen() == "fourth"
    view = {item["name"]: item for item in (await hub.codex_profiles())["profiles"]}
    assert view["third"]["limited_until"] == 1_793_599_750_000 < local_ms(2026, 11, 3, 20, 2)


async def test_while_every_account_is_limited_the_walk_is_tried_again_after_30_minutes(hub):
    async def chosen():
        return (await hub.codex_session_profile())["name"]

    async def hold():
        return await hub.db.read(lambda c: hub._dispatch_hold(c, "codex"))

    hub.limits_replies.update(codex=AT_LIMIT, second=AT_LIMIT, third=AT_LIMIT, fourth=AT_LIMIT)
    # The first use checks main, which is at its limit; the walk then finds every one at its limit.
    assert await chosen() == "main" and hub.limits_reads == ["codex", "codex", "second", "third", "fourth"]
    assert (await hold())["until"] == NOW + 1_800_000
    assert await chosen() == "main" and len(hub.limits_reads) == 5  # No walk while new sessions wait.
    hub.now[0] = NOW + 1_800_001
    hub.limits_replies["fourth"] = WITHIN
    assert await hold() is None and await chosen() == "fourth" and await hold() is None
    history = [(item["kind"], item["profile"]) for item in reversed((await hub.codex_profiles())["history"])]
    assert history.count(("all_limited", None)) == 1 and history[-1] == ("selected", "fourth")
    # Putting a profile back in the rotation ends a wait at once: the next session walks again.
    for name in NAMES:
        await hub.codex_limit_reached(name, LIMIT.format(when="Nov 2nd, 2026 1:00 AM"))
    hub.limits_replies["fourth"] = AT_LIMIT
    await chosen()
    assert await hold() is not None
    await command(hub, "runtime.set_codex_profile_rotation", {"profile": "second", "in_rotation": True})
    assert await hold() is None
