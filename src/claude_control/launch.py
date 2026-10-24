"""Starting Claude Code sessions in tmux (launch_session)."""

import os
import secrets
import shlex
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import tmux
from .state import Tracker

TMUX_SESSION = "claude"
SHELL = "bash -li"


class LaunchError(Exception):
    pass


def ansi_c(text: str) -> str:
    """Quote `text` as one bash $'...' word, so a multi-line prompt types as a single line."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "$'" + "".join(out) + "'"


def command_line(
    claude_bin: str, session_id: str, name: str, prompt: str | None, extra_args: Iterable[str]
) -> str:
    args = [claude_bin, "--session-id", session_id, "--name", name, *extra_args]
    line = shlex.join(args)
    return f"{line} {ansi_c(prompt)}" if prompt else line


def default_name(cwd: str, taken: set[str]) -> str:
    """cwd basename plus two hex digits, like Claude Code's own names."""
    base = Path(cwd).name or "home"
    for _ in range(64):
        name = f"{base}-{secrets.token_hex(1)}"
        if name not in taken:
            return name
    return f"{base}-{secrets.token_hex(3)}"


def find_process(session_id: str, proc: Path = Path("/proc")) -> int | None:
    """Pid of the process started with `--session-id <session_id>`, if it runs."""
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        target = session_id.encode()
        for i, arg in enumerate(argv[:-1]):
            if arg == b"--session-id" and argv[i + 1] == target:
                return int(entry.name)
    return None


NewWindow = Callable[[str, str, str], Awaitable[str]]  # (window name, cwd, command) -> pane
TypeLine = Callable[[str, str], Awaitable[None]]  # (pane, text)


@dataclass
class Launcher:
    tracker: Tracker
    claude_bin: str = "claude"
    extra_args: tuple[str, ...] = ()
    new_window: NewWindow | None = None
    type_line: TypeLine | None = None
    tmux_socket: str | None = None

    async def __call__(
        self, cwd: str, prompt: str | None, name: str | None, now: float
    ) -> dict[str, Any]:
        path = os.path.expanduser(cwd.strip())
        if not os.path.isabs(path):
            raise LaunchError(f"cwd must be an absolute path: {cwd!r}")
        if not os.path.isdir(path):
            raise LaunchError(f"no such directory: {path}")
        path = os.path.normpath(path)
        live_names = {
            s.name for s in self.tracker.sessions.values() if s.name and s.state != "ended"
        }
        name = (name or "").strip() or default_name(path, live_names)
        if name in live_names:
            raise LaunchError(f"a session named {name!r} is already running")
        session_id = str(uuid.uuid4())

        new_window = self.new_window or self._tmux_new_window
        type_line = self.type_line or self._tmux_type_line
        try:
            pane = await new_window(name, path, SHELL)
        except tmux.TmuxError as e:
            raise LaunchError(str(e)) from e
        # Registered before the command is typed, so its SessionStart finds the session.
        self.tracker.launching(session_id, name, path, pane, now)
        line = command_line(self.claude_bin, session_id, name, prompt, self.extra_args)
        try:
            await type_line(pane, line)
        except tmux.TmuxError as e:
            raise LaunchError(f"window opened but the command could not be typed: {e}") from e
        return {"session_id": session_id, "name": name, "pane": pane, "state": "starting"}

    async def _tmux_new_window(self, window: str, cwd: str, command: str) -> str:
        return await tmux.new_window(TMUX_SESSION, window, cwd, command, self.tmux_socket)

    async def _tmux_type_line(self, pane: str, text: str) -> None:
        await tmux.type_line(pane, text, self.tmux_socket)
