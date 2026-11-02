from pathlib import Path

from formal_geyser.coordination.config import initialize, load_config, resolve_config
from formal_geyser.coordination.models import CodexConfig, ProjectConfig
from formal_geyser.plugin import LeanPlugin


def test_lean_default_is_the_application_home():
    assert Path(LeanPlugin().default_codex_home()) == Path.home() / ".local/share/formal-geyser/codex"


def test_lean_initialization_and_legacy_config_read_select_application_home(tmp_path, monkeypatch):
    home = str((tmp_path / "auto-lean").resolve())
    monkeypatch.setattr(LeanPlugin, "default_codex_home", lambda self: home)
    root = tmp_path / "project"
    initialize(root, ProjectConfig(domain="lean"))
    assert load_config(root).codex.home == home
    legacy = root / "legacy"
    legacy.mkdir()
    (legacy / "project.toml").write_text('domain = "lean"\n', encoding="utf-8")
    assert load_config(legacy).codex.home == home
    assert not Path(home).exists()


def test_lean_explicit_home_override_is_preserved(tmp_path):
    override = str((tmp_path / "operator-selected").resolve())
    config = resolve_config(tmp_path, ProjectConfig(domain="lean", codex=CodexConfig(home=override)))
    assert config.codex.home == override
    assert not Path(override).exists()
