import tomllib
from pathlib import Path

import tomli_w

from .literature import LiteratureFiles
from .models import ProjectConfig
from .wiki import ensure_guides


def resolve_config(path: Path, config: ProjectConfig) -> ProjectConfig:
    """Resolve optional domain defaults without creating a home or changing process state."""
    update = {}
    for backend, provider_name in (("codex", "default_codex_home"), ("claude", "default_claude_home")):
        section = config.backend_config(backend)
        home = section.home
        if home is None:
            from .plugins import load_plugin
            provider = getattr(load_plugin(config.domain), provider_name, None)
            home = provider() if callable(provider) else None
        if home is None:
            continue
        if not isinstance(home, str) or not home.strip():
            raise ValueError(f"{backend.capitalize()} home must be a nonempty path")
        resolved = Path(home).expanduser()
        if not resolved.is_absolute():
            resolved = path.resolve() / resolved
        update[backend] = section.model_copy(update={"home": str(resolved.resolve())})
    return config.model_copy(update=update) if update else config


def codex_profiles_equal(left, right) -> bool:
    """The same account, home, model and session settings."""
    # The RPC receive envelope is host transport, not account/model/session identity.
    # Raising it must not invalidate an otherwise exact saved interruption ticket. Nor does the
    # list of sign-ins a conversation may move between (profiles, see account_profiles).
    def identity(value):
        return {key: item for key, item in value.items() if key not in {"max_frame_bytes", "profiles"}}
    return isinstance(left, dict) and isinstance(right, dict) and identity(left) == identity(right)


def initialize(path: Path, config: ProjectConfig) -> Path:
    path = path.resolve()
    config = resolve_config(path, config)
    path.mkdir(parents=True, exist_ok=True)
    target = path / "project.toml"
    if target.exists():
        raise FileExistsError(target)
    state = path / ".task-based"
    if state.exists() and any(state.iterdir()):
        raise FileExistsError("Refusing to initialize over existing project state")
    state.mkdir(exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(tomli_w.dumps(config.model_dump(exclude_none=True)))
    ensure_guides(path)
    if config.domain == "lean":
        literature = LiteratureFiles(path)
        literature.ensure_index()
    return target


def load_config(path: Path) -> ProjectConfig:
    with (path / "project.toml").open("rb") as stream:
        record = tomllib.load(stream)
    return resolve_config(path, ProjectConfig.model_validate(record))
