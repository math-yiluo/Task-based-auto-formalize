"""The workers' permission profile, and the native access preflight: a real sandboxed command
checks reads, refused reads and writes, and the workspace."""
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

from formal_geyser.coordination import native_permissions
from formal_geyser.coordination.models import CodexConfig

# The probe's HTTPS request fails at once: no test reaches the network.
OFFLINE = {"https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}


def unsandboxed_codex(tmp_path):
    """A stand-in for `codex sandbox` that runs the command without any sandbox."""
    codex = tmp_path / "codex"
    codex.write_text(f"#!{sys.executable}\nimport os, sys\n"
                     "command = sys.argv[sys.argv.index('--') + 1:]\nos.execv(command[0], command)\n")
    codex.chmod(0o755)
    return codex


def test_the_worker_profile_denies_protected_paths_and_leaves_tmp_readable():
    overrides = native_permissions.worker_profile(["/run/user/1000", "/home/u/.codex/auth.json"])
    values = dict(item.split("=", 1) for item in overrides[1::2])
    assert overrides[::2] == ["-c"] * 3
    assert tomllib.loads("value = " + values["permissions.taskhub-worker.extends"])["value"] == ":workspace"
    assert tomllib.loads("value = " + values["permissions.taskhub-worker.filesystem"])["value"] == {
        ":slash_tmp": "read", "/run/user/1000": "deny", "/home/u/.codex/auth.json": "deny"}
    assert values["permissions.taskhub-worker.network.enabled"] == "true"


def test_protected_reads_are_the_runtime_directory_and_existing_sign_ins(tmp_path, monkeypatch):
    runtime, codex, claude = tmp_path / "runtime", tmp_path / "codex", tmp_path / "claude"
    for directory in (runtime, codex, claude):
        directory.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(native_permissions, "windows_drives", lambda: [])
    assert native_permissions.protected_reads(str(codex), str(claude)) == [str(runtime)]
    (codex / "auth.json").write_text("{}")
    (claude / ".credentials.json").write_text("{}")
    assert native_permissions.protected_reads(str(codex), str(claude)) == [
        str(runtime), str(codex / "auth.json"), str(claude / ".credentials.json")]


MOUNTS = r"""C:\134 /mnt/c 9p rw,noatime,aname=drvfs;path=C:\;uid=1000 0 0
drivers /usr/lib/wsl/drivers 9p ro,nosuid,aname=drivers 0 0
D: /mnt/d drvfs rw 0 0
/dev/sdc / ext4 rw,relatime 0 0
"""


def test_windows_drives_are_the_drvfs_mounts():
    assert native_permissions.windows_drives(MOUNTS) == ["/mnt/c", "/mnt/d"]


def test_wsl_interop_fails_the_preflight_whatever_the_probe_found(tmp_path, monkeypatch):
    def run(argv, **kwargs):
        checks = {**dict.fromkeys(native_permissions.REQUIRED, True), "network": True}
        return SimpleNamespace(returncode=0, stdout=json.dumps(checks).encode(), stderr=b"")
    monkeypatch.setattr(native_permissions.subprocess, "run", run)
    monkeypatch.setattr(native_permissions, "wsl_interop", lambda: True)
    result = native_permissions.native_access_preflight(tmp_path, tmp_path / "work", CodexConfig(executable=sys.executable))
    assert not result["ok"] and "interop" in result["blockers"][0]


def test_an_unreachable_network_is_a_warning_not_a_failed_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(native_permissions, "wsl_interop", lambda: False)

    def run(argv, **kwargs):
        checks = {**dict.fromkeys(native_permissions.REQUIRED, True), "network": False,
                  "network_error": "unreachable"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(checks).encode(), stderr=b"")
    monkeypatch.setattr(native_permissions.subprocess, "run", run)
    result = native_permissions.native_access_preflight(tmp_path, tmp_path / "work", CodexConfig(executable=sys.executable))
    assert result["ok"] and "unreachable" in result["warnings"][0]


def test_the_preflight_runs_the_worker_profile(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
    monkeypatch.setattr(native_permissions.subprocess, "run", run)
    secret = tmp_path / "auth.json"
    secret.write_text("{}")
    native_permissions.native_access_preflight(tmp_path, tmp_path / "work", CodexConfig(executable="/usr/bin/codex"),
                                               [str(secret)])
    argv = calls[0]
    sandbox = argv.index("sandbox")
    assert argv[1:sandbox] == native_permissions.worker_profile([str(secret)])
    assert argv[sandbox:sandbox + 5] == ["sandbox", "-P", "taskhub-worker", "-C", str(tmp_path / "work")]


def test_without_a_sandbox_the_preflight_fails_and_leaves_no_sentinel(tmp_path, monkeypatch):
    for key, value in OFFLINE.items():
        monkeypatch.setenv(key, value)
    root, runtime = tmp_path / "project", tmp_path / "runtime"
    runtime.mkdir()
    secret = tmp_path / "auth.json"
    secret.write_text("{}")
    result = native_permissions.native_access_preflight(
        root, root / ".task-based/native-access-probe/work", CodexConfig(executable=str(unsandboxed_codex(tmp_path))),
        [str(runtime), str(secret)])
    checks = result["checks"]
    assert not result["ok"] and checks["library_read"] and checks["workspace_write"]
    assert checks["library_write_denied"] is False and checks["root_write_denied"] is False
    assert checks["tmp_write_denied"] is False
    assert checks["protected_0_read_denied"] is False and checks["protected_1_read_denied"] is False
    assert not [path for path in root.rglob(".write-probe-*")] and not list(runtime.iterdir())


def test_a_refused_read_or_other_content_counts_as_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(native_permissions, "wsl_interop", lambda: False)
    runtime, secret = tmp_path / "runtime", tmp_path / "auth.json"
    runtime.mkdir()
    secret.write_text('{"token": "never read"}')

    def run(argv, **kwargs):
        # A sandbox that hides the runtime directory's file behind an empty one and refuses the other.
        checks = {**dict.fromkeys(native_permissions.REQUIRED, True), "network": True,
                  "protected_0_read": hashlib.sha256(b"").hexdigest(), "protected_1_read": None}
        return SimpleNamespace(returncode=0, stdout=json.dumps(checks).encode(), stderr=b"")
    monkeypatch.setattr(native_permissions.subprocess, "run", run)
    result = native_permissions.native_access_preflight(
        tmp_path / "project", tmp_path / "work", CodexConfig(executable=sys.executable), [str(runtime), str(secret)])
    checks = result["checks"]
    assert result["ok"] and checks["protected_0_read_denied"] is True and checks["protected_1_read_denied"] is True
    assert "protected_0_read" not in checks and "protected_1_read" not in checks


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through permissions")
def test_an_unwritable_protected_directory_is_checked_through_a_file_already_in_it(tmp_path, monkeypatch):
    for key, value in OFFLINE.items():
        monkeypatch.setenv(key, value)
    root, drive, bare = tmp_path / "project", tmp_path / "drive", tmp_path / "bare"
    (drive / "Windows").mkdir(parents=True)
    (drive / "pagefile.sys").write_bytes(b"\0" * (2 << 20))
    (drive / "Windows" / "win.ini").write_text("[fonts]\n")
    bare.mkdir()
    for directory in (drive, bare):
        directory.chmod(0o555)
    try:
        result = native_permissions.native_access_preflight(
            root, root / ".task-based/native-access-probe/work", CodexConfig(executable=str(unsandboxed_codex(tmp_path))),
            [str(drive), str(bare)])
    finally:
        for directory in (drive, bare):
            directory.chmod(0o755)
    inputs = json.loads((root / ".task-based/native-access-check/inputs.json").read_text())
    assert inputs["protected_reads"] == {"protected_0": str(drive / "Windows" / "win.ini")}
    # Without a sandbox the file is read, so the preflight fails; the bare directory cannot be checked.
    assert result["checks"]["protected_0_read_denied"] is False and not result["ok"]
    assert "protected_1_read_denied" not in result["checks"]
    assert any(str(bare) in warning for warning in result["warnings"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and writes through permissions")
def test_the_probe_counts_a_refused_write_as_denied_and_fingerprints_what_it_reads(tmp_path):
    readable, locked, workspace = tmp_path / "readable.lean", tmp_path / "locked", tmp_path / "work"
    readable.write_text("theorem t : True := trivial\n", encoding="utf-8")
    locked.mkdir()
    workspace.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("never read")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({
        "library": str(readable), "staging": str(readable), "required": [], "read_bytes": 65536,
        "protected_reads": {"secret": str(secret), "open": str(readable)},
        "protected_writes": {"locked": str(locked / "probe"), "missing": str(tmp_path / "absent" / "probe")},
        "workspace_write": str(workspace / "probe.txt")}), encoding="utf-8")
    locked.chmod(0o555)
    secret.chmod(0o000)
    try:
        completed = subprocess.run([sys.executable, "-I", "-c", native_permissions.PROBE, str(inputs)],
                                   capture_output=True, text=True, timeout=60, env={**os.environ, **OFFLINE})
    finally:
        locked.chmod(0o755)
        secret.chmod(0o600)
    checks = json.loads(completed.stdout)
    # A read-only directory refuses with EACCES; a read-only mount would with EROFS.
    assert checks["locked_write_denied"] is True and checks["missing_write_denied"] is False
    assert checks["secret_read"] is None and checks["open_read"] == hashlib.sha256(readable.read_bytes()).hexdigest()
    assert checks["library_read"] and checks["workspace_write"] and checks["network"] is False
