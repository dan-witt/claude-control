"""tmux commands. A server is only ever started in its own systemd scope (DESIGN.md, "tmux").

`socket` selects a server with `tmux -L` (tests and the dev harness); None is the default server.
"""

import asyncio
import shlex
import subprocess

TIMEOUT_S = 5
SCOPE_UNIT = "claude-tmux"


class TmuxError(Exception):
    pass


def _prefix(socket: str | None) -> list[str]:
    return ["tmux", *(["-L", socket] if socket else [])]


async def _exec(argv: list[str]) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise TmuxError(f"{argv[0]} not available: {e}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TmuxError(f"{shlex.join(argv[:3])} timed out") from None
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode(errors="replace").strip()


async def _tmux(socket: str | None, *args: str) -> str:
    code, out, err = await _exec([*_prefix(socket), *args])
    if code != 0:
        raise TmuxError(err or f"tmux {args[0]} failed")
    return out


async def focus(pane: str, socket: str | None = None) -> str:
    """Switch the most recently active tmux client to `pane`; returns the client name.

    `switch-client -t <pane id>` selects the pane's session, window and pane (tmux 3.4).
    """
    listing = await _tmux(socket, "list-clients", "-F", "#{client_activity} #{client_name}")
    clients = []
    for line in listing.splitlines():
        activity, _, name = line.partition(" ")
        if activity.isdigit() and name:
            clients.append((int(activity), name))
    if not clients:
        raise TmuxError("no tmux client is attached")
    client = max(clients)[1]
    await _tmux(socket, "switch-client", "-c", client, "-t", pane)
    return client


async def new_window(
    session: str, window: str, cwd: str, command: str, socket: str | None = None
) -> str:
    """Open a window running `command` in `cwd`, creating the session (and server) if needed.

    Returns the new pane's id. The window is created detached, so no client moves to it.
    """
    tmux = _prefix(socket)
    code, _, _ = await _exec([*tmux, "has-session", "-t", f"={session}"])
    if code == 0:
        argv = [*tmux, "new-window", "-d", "-t", f"={session}:", "-n", window, "-c", cwd]
    else:
        argv = [*tmux, "new-session", "-d", "-s", session, "-n", window, "-c", cwd]
        server_running = (await _exec([*tmux, "list-sessions"]))[0] == 0
        if not server_running and socket is None:
            scope = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={SCOPE_UNIT}"]
            argv = [*scope, *argv]
    code, out, err = await _exec([*argv, "-P", "-F", "#{pane_id}", command])
    if code != 0:
        raise TmuxError(err or "could not open a tmux window")
    return out.strip()


async def type_line(pane: str, text: str, socket: str | None = None) -> None:
    """Type `text` into `pane` literally and press Enter."""
    await _tmux(socket, "send-keys", "-t", pane, "-l", text)
    await _tmux(socket, "send-keys", "-t", pane, "Enter")


async def pane_pids(socket: str | None = None) -> dict[int, str]:
    """pane_pid -> pane id for every pane; empty when no server runs."""
    try:
        out = await _tmux(socket, "list-panes", "-a", "-F", "#{pane_pid} #{pane_id}")
    except TmuxError:
        return {}
    panes = {}
    for line in out.splitlines():
        pid, _, pane = line.partition(" ")
        if pid.isdigit() and pane:
            panes[int(pid)] = pane
    return panes


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def ensure_window(
    session: str, window: str, command: list[str], socket: str | None = None
) -> list[list[str]]:
    """Make sure tmux session `session` has a window named `window` running `command`.

    Synchronous, for the CLI. Returns the commands it ran.
    """
    tmux = _prefix(socket)
    shell_command = shlex.join(command)
    ran: list[list[str]] = []

    def run(argv: list[str]) -> None:
        ran.append(argv)
        result = _run(argv)
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or f"{argv[0]} failed")

    if _run([*tmux, "has-session", "-t", f"={session}"]).returncode != 0:
        new = [*tmux, "new-session", "-d", "-s", session, "-n", window, shell_command]
        server_running = _run([*tmux, "list-sessions"]).returncode == 0
        if not server_running and socket is None:
            new = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={SCOPE_UNIT}", *new]
        run(new)
        return ran
    windows = _run([*tmux, "list-windows", "-t", f"={session}", "-F", "#{window_name}"])
    if window not in windows.stdout.split():
        run([*tmux, "new-window", "-d", "-t", f"={session}:", "-n", window, shell_command])
    return ran
