import os
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path

APP = "claude-control"


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 7777
    retention_days: int = 14
    replay_hours: int = 24
    reconcile_interval_s: float = 15
    claude_bin: str = "claude"
    # Extra `claude` arguments for launch_session, e.g. ["--model", "opus"].
    launch_args: tuple[str, ...] = ()
    # tmux -L socket name; None is the default server. For the dev harness.
    tmux_socket: str | None = None

    @property
    def hooks_url(self) -> str:
        return f"http://{self.host}:{self.port}/hooks"

    @property
    def db_path(self) -> Path:
        return state_dir() / "events.db"


def token_path() -> Path:
    return config_dir() / "token"


def ensure_token() -> str:
    """Bearer token for /mcp, generated on first use (mode 0600)."""
    path = token_path()
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def load() -> Config:
    path = config_dir() / "config.toml"
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    known = {k: v for k, v in data.items() if k in Config.__dataclass_fields__}
    if "launch_args" in known:
        known["launch_args"] = tuple(str(a) for a in known["launch_args"])
    return Config(**known)
