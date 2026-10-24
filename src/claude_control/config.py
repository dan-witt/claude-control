import os
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

    @property
    def hooks_url(self) -> str:
        return f"http://{self.host}:{self.port}/hooks"

    @property
    def db_path(self) -> Path:
        return state_dir() / "events.db"


def load() -> Config:
    path = config_dir() / "config.toml"
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    known = {k: v for k, v in data.items() if k in Config.__dataclass_fields__}
    return Config(**known)
