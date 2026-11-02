"""The workers' Codex permission profile, and a native probe of it: project content is
readable, only the workspace is writable, and protected paths cannot be read."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence
from uuid import uuid4

# Codex's workspace profile (read everything, write the thread's workspace roots) with network
# access, /tmp only for reading, and no reading of what protected_reads names.
WORKER_PROFILE = "taskhub-worker"


def windows_drives(mounts: str | None = None) -> list[str]:
    """The Windows drives WSL mounts, which hold the Windows side's files and sign-ins: from
    `mounts`, in the format of /proc/self/mounts, or this process's own."""
    if mounts is None:
        try:
            mounts = Path("/proc/self/mounts").read_text()
        except OSError:
            return []
    rows = [line.split() for line in mounts.splitlines()]
    return [fields[1] for fields in rows if len(fields) >= 4 and (
        fields[2] == "drvfs" or (fields[2] == "9p" and "aname=drvfs" in fields[3]))]


def wsl_interop() -> bool:
    """Whether WSL runs Windows programs: a sandboxed command could start one outside the sandbox."""
    for path in Path("/proc/sys/fs/binfmt_misc").glob("WSLInterop*"):
        try:
            if path.read_text().startswith("enabled"):
                return True
        except OSError:
            continue
    return False


INTEROP = ("WSL interop is on: turn it off ([interop] enabled=false in /etc/wsl.conf), or a sandboxed "
           "command can start Windows programs outside the sandbox")


def sandbox_blocker() -> str | None:
    """Why agents' commands cannot be sandboxed on this machine now, or None. WSL can turn
    interop back on under a running service, as when binfmt_misc is mounted again."""
    return INTEROP if wsl_interop() else None


def protected_reads(codex_home: str | Sequence[str] | None, claude_home: str | Sequence[str] | None) -> list[str]:
    """What no sandboxed command may read: the user's runtime directory (the operator tokens,
    the systemd and D-Bus sockets), each backend's stored sign-in (every profile's), and
    Windows drives."""
    def homes(value: str | Sequence[str] | None) -> list[str | None]:
        listed: list[str | None] = [value] if isinstance(value, str) else [*(value or ())]
        return listed or [None]  # The backend's own default directory.
    candidates = [Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"),
                  *((Path(home).expanduser() if home else Path.home() / ".codex") / "auth.json"
                    for home in homes(codex_home)),
                  *((Path(home).expanduser() if home else Path.home() / ".claude") / ".credentials.json"
                    for home in homes(claude_home)),
                  *map(Path, windows_drives())]
    # A sandbox hides only what exists.
    return list(dict.fromkeys(str(path.resolve()) for path in candidates if path.exists()))


def worker_profile(protected: Sequence[str]) -> list[str]:
    """The `-c` overrides that define WORKER_PROFILE."""
    # Codex names /tmp `:slash_tmp`; a "/tmp" entry would leave the base profile's write access.
    access = {":slash_tmp": "read", **dict.fromkeys(protected, "deny")}
    table = "{" + ", ".join(f"{json.dumps(path)} = {json.dumps(mode)}" for path, mode in access.items()) + "}"
    name = "permissions." + WORKER_PROFILE
    return ["-c", name + '.extends=":workspace"', "-c", name + ".filesystem=" + table,
            "-c", name + ".network.enabled=true"]


PROBE = '''import errno,hashlib,json,pathlib,sys
p=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf8"))
r={}
for key in ("library", "staging"):
    try:
        pathlib.Path(p[key]).read_bytes(); r[key+"_read"]=True
    except OSError as e: r[key+"_read"]=False; r[key+"_error"]=str(e)
for key, target in p["protected_reads"].items():
    try:
        with open(target,"rb") as f: r[key+"_read"]=hashlib.sha256(f.read(p["read_bytes"])).hexdigest()
    except OSError: r[key+"_read"]=None
for key, target in p["protected_writes"].items():
    try:
        pathlib.Path(target).write_text("write probe",encoding="utf8")
        r[key+"_write_denied"]=False
    except OSError as e: r[key+"_write_denied"]=e.errno in (errno.EACCES,errno.EPERM,errno.EROFS)
try:
    pathlib.Path(p["workspace_write"]).write_text("download destination probe",encoding="utf8")
    r["workspace_write"]=True
except OSError as e: r["workspace_write"]=False; r["workspace_error"]=str(e)
try:
    import urllib.request,urllib.error
    try:
        with urllib.request.urlopen(urllib.request.Request("https://arxiv.org",method="HEAD"),timeout=15) as response:
            r["http_status"]=response.status
        r["network"]=True
    except urllib.error.HTTPError as e:
        r["network"]=True; r["http_status"]=e.code
    r["native_downloader"]="Python urllib/OpenSSL"
except OSError as e: r["network"]=False; r["network_error"]=str(e)
print(json.dumps(r))
sys.exit(0 if all(r.get(k) is True for k in p["required"]) else 1)
'''
# Only the probe's own workspace may be writable: shared trees, the project root,
# coordinator state, correspondence and /tmp must all refuse sandbox writes.
PROTECTED_WRITES = {"library": "library", "root": "", "state": ".task-based", "correspondence": "correspondence"}
# Network access is checked and reported, but an unreachable site or network does not stop work.
REQUIRED = ("library_read", "staging_read", "workspace_write", "tmp_write_denied",
            *(name + "_write_denied" for name in PROTECTED_WRITES))
# How much of a protected file the probe reads and fingerprints.
READ_BYTES = 65536


def _fingerprint(path: Path) -> str | None:
    """The digest of a file's first READ_BYTES, or None when nothing in it can be read."""
    try:
        with path.open("rb") as handle:
            data = handle.read(READ_BYTES)
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest() if data else None


def _existing_file(directory: Path, limit: int = 256) -> Path | None:
    """A small readable file already in a directory, or one level down, for a protected
    directory the preflight cannot write a probe file in."""
    pending, examined = [(directory, 0)], 0
    while pending:
        folder, depth = pending.pop(0)
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            examined += 1
            if examined > limit:
                return None
            try:
                if entry.is_file(follow_symlinks=False) and entry.stat().st_size <= 1 << 20:
                    if _fingerprint(Path(entry.path)):
                        return Path(entry.path)
                elif depth == 0 and entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), 1))
            except OSError:
                continue
    return None


def native_access_preflight(root: Path, workspace: Path, config, protected: Sequence[str] = ()) -> dict:
    """A real Codex sandboxed command in WORKER_PROFILE; no model, login, or paid turn."""
    workspace.mkdir(parents=True, exist_ok=True)
    directory = root / ".task-based" / "native-access-check"
    directory.mkdir(parents=True, exist_ok=True)
    downloads = workspace / ".private" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    probe = directory / "probe.py"
    probe.write_text(PROBE, encoding="utf8")
    paths: dict[str, object] = {}
    for name in ("library", "staging"):
        candidates = (root / name).rglob("*.lean")
        target = next(candidates, None)
        if target is None:
            target = root / name / ".access-probe" / "read.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Native read preflight.\n", encoding="utf8")
        paths[name] = str(target)
    sentinel = ".write-probe-" + uuid4().hex
    for folder in PROTECTED_WRITES.values():
        (root / folder).mkdir(parents=True, exist_ok=True)
    writes = {name: str(root / folder / sentinel) for name, folder in PROTECTED_WRITES.items()}
    writes["tmp"] = str(Path("/tmp") / sentinel)
    # A protected file is read as it is; a protected directory, through a file placed in it or,
    # where the preflight cannot write, one already there.
    reads, fingerprints, placed, unverified = {}, {}, [], []
    for index, path in enumerate(protected):
        probed: Path | None = Path(path)
        if Path(path).is_dir():
            probe_file = Path(path) / (".read-probe-" + uuid4().hex)
            try:
                probe_file.write_text("read probe", encoding="utf8")
                placed.append(probe_file)
                probed = probe_file
            except OSError:
                probed = _existing_file(Path(path))
        fingerprint = _fingerprint(probed) if probed is not None else None
        if fingerprint is None:
            unverified.append(path)
            continue
        reads[f"protected_{index}"], fingerprints[f"protected_{index}"] = str(probed), fingerprint
    required = [*REQUIRED, *(key + "_read_denied" for key in reads)]
    paths.update(protected_writes=writes, protected_reads=reads, read_bytes=READ_BYTES, required=list(REQUIRED),
                 workspace_write=str(downloads / "access-probe.txt"))
    inputs = directory / "inputs.json"
    inputs.write_text(json.dumps(paths), encoding="utf8")
    env = dict(os.environ)
    if config.home:
        env["CODEX_HOME"] = config.home
    argv = [config.executable, *worker_profile(protected), "sandbox", "-P", WORKER_PROFILE, "-C", str(workspace),
            "--", sys.executable, "-I", str(probe), str(inputs)]
    try:
        completed = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, timeout=90)
    finally:
        for probe_file in placed:
            probe_file.unlink(missing_ok=True)
    blockers = [INTEROP] if wsl_interop() else []
    result = {"ok": False, "returncode": completed.returncode, "protected_reads": list(protected),
              "stderr": completed.stderr.decode("utf8", errors="replace")[-2000:], "blockers": blockers}
    warnings = [f"Could not check that {path} is unreadable: the preflight can neither write a file in it "
                "nor read one there" for path in unverified]
    for line in completed.stdout.decode("utf8", errors="replace").splitlines():
        try:
            checks = json.loads(line)
        except ValueError:
            continue
        if isinstance(checks, dict):
            for key, fingerprint in fingerprints.items():
                # A sandbox may hide a file behind an empty one: only reading its content counts.
                checks[key + "_read_denied"] = checks.pop(key + "_read", fingerprint) != fingerprint
            result["checks"] = checks
            result["ok"] = (completed.returncode == 0 and all(checks.get(k) is True for k in required)
                            and not blockers)
            if checks.get("network") is not True:
                warnings.insert(0, "HTTPS to arxiv.org failed: " + str(checks.get("network_error", "unknown")))
    if warnings:
        result["warnings"] = warnings
    (directory / "latest.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    # Only disposable sentinels can have been written if protection failed.
    for written in writes.values():
        Path(written).unlink(missing_ok=True)
    return result


async def ensure_native_access(root: Path, workspace: Path, config, protected: Sequence[str] = ()) -> dict:
    result = await asyncio.to_thread(native_access_preflight, root, workspace, config, protected)
    if not result["ok"]:
        raise RuntimeError("Native access preflight failed; see .task-based/native-access-check/latest.json")
    return result
