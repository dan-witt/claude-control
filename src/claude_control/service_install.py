import subprocess
import sys
from pathlib import Path

UNIT_NAME = "claude-control.service"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def executable() -> str:
    # Not resolved: ~/.local/bin/claude-control is a symlink that survives `uv tool install` upgrades.
    return str(Path(sys.argv[0]).absolute())


def unit_text(exe: str) -> str:
    return f"""[Unit]
Description=claude-control: Claude Code session daemon

[Service]
ExecStart={exe} serve
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def install(exe: str, start: bool = True) -> Path:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(exe))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if start:
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=True)
    return path
