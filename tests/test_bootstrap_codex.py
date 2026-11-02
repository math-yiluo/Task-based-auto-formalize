from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from formal_geyser import bootstrap_codex


def install_probe(monkeypatch, tmp_path, *, version="0.160.0", returncode=0, stderr=""):
    calls = []
    native = tmp_path / "native" / "codex"
    native.parent.mkdir()
    native.write_bytes(b"synthetic native executable")

    def resolve(value):
        calls.append(("resolve", value))
        return str(native)

    def run(command, **kwargs):
        calls.append(("run", command, kwargs))
        return SimpleNamespace(returncode=returncode, stdout="codex-cli " + version, stderr=stderr)

    monkeypatch.setattr(bootstrap_codex, "resolve_codex_executable", resolve)
    # Scope the probe double to this module.
    monkeypatch.setattr(bootstrap_codex, "subprocess", SimpleNamespace(
        run=run, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(bootstrap_codex, "_application_root", lambda: tmp_path)
    return native, calls


def test_new_instance_prefers_existing_application_runtime_over_global_path(tmp_path, monkeypatch):
    native, calls = install_probe(monkeypatch, tmp_path)
    local = bootstrap_codex._bundled_executables()[0]
    local.parent.mkdir(parents=True)
    local.write_bytes(b"installed runtime")
    assert bootstrap_codex.select_bootstrap_codex() == (str(native), "0.160.0")
    assert calls[0] == ("resolve", str(local))
    assert calls[1][1] == [str(native), "--version"]
    assert calls[1][2]["timeout"] == 10


def test_explicit_selection_wins_over_application_default(tmp_path, monkeypatch):
    native, calls = install_probe(monkeypatch, tmp_path, version="0.160.0")
    local = bootstrap_codex._bundled_executables()[0]
    local.parent.mkdir(parents=True)
    local.write_bytes(b"installed runtime")
    assert bootstrap_codex.select_bootstrap_codex("selected-native") == (str(native), "0.160.0")
    assert calls[0] == ("resolve", "selected-native")


def test_supported_global_runtime_is_used_when_no_application_copy_exists(tmp_path, monkeypatch):
    native, calls = install_probe(monkeypatch, tmp_path, version="0.160.0")
    assert bootstrap_codex.select_bootstrap_codex() == (str(native), "0.160.0")
    assert calls[0] == ("resolve", "codex")
    assert not (tmp_path / ".tools").exists()


@pytest.mark.parametrize("version", ["0.144.6", "0.161.0", "0.999.0"])
def test_unaudited_runtime_is_not_silently_adopted_or_installed(tmp_path, monkeypatch, version):
    _, calls = install_probe(monkeypatch, tmp_path, version=version)
    with pytest.raises(ValueError, match="is not the audited runtime version 0.160.0"):
        bootstrap_codex.select_bootstrap_codex()
    assert len(calls) == 2
    assert not (tmp_path / ".tools").exists()
    assert not (tmp_path / "project.toml").exists()


@pytest.mark.parametrize("version,returncode", [("unrecognized", 0), ("0.160.0", 1)])
def test_failed_or_unrecognized_version_is_not_accepted(tmp_path, monkeypatch, version, returncode):
    install_probe(monkeypatch, tmp_path, version=version, returncode=returncode)
    with pytest.raises(ValueError, match="did not report a successful version"):
        bootstrap_codex.select_bootstrap_codex()


def test_probe_failure_keeps_existing_files_unchanged(tmp_path, monkeypatch):
    install_probe(monkeypatch, tmp_path)
    config = tmp_path / "project.toml"
    config.write_text("existing immutable configuration", encoding="utf-8")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex", 10)

    monkeypatch.setattr(bootstrap_codex.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="could not report its version"):
        bootstrap_codex.select_bootstrap_codex()
    assert config.read_text(encoding="utf-8") == "existing immutable configuration"
