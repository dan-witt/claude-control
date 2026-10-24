"""tmux commands the daemon runs. None of them starts a tmux server."""

import asyncio
import shlex
import subprocess

TIMEOUT_S = 5


class TmuxError(Exception):
    pass


async def _tmux(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TmuxError(f"tmux {args[0]} timed out") from None
    if proc.returncode != 0:
        raise TmuxError(err.decode(errors="replace").strip() or f"tmux {args[0]} failed")
    return out.decode()


async def focus(pane: str) -> str:
    """Switch the most recently active tmux client to `pane`; returns the client name.

    `switch-client -t <pane id>` selects the pane's session, window and pane (tmux 3.4).
    """
    try:
        listing = await _tmux("list-clients", "-F", "#{client_activity} #{client_name}")
    except OSError as e:
        raise TmuxError(f"tmux not available: {e}") from e
    clients = []
    for line in listing.splitlines():
        activity, _, name = line.partition(" ")
        if activity.isdigit() and name:
            clients.append((int(activity), name))
    if not clients:
        raise TmuxError("no tmux client is attached")
    client = max(clients)[1]
    await _tmux("switch-client", "-c", client, "-t", pane)
    return client


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def ensure_window(
    session: str, window: str, command: list[str], socket: str | None = None
) -> list[list[str]]:
    """Make sure tmux session `session` has a window named `window` running `command`.

    A tmux server started here gets its own systemd scope, so it outlives the terminal and
    is not in any service's cgroup. Returns the commands it ran. `socket` (tmux -L) is for
    tests.
    """
    tmux = ["tmux", *(["-L", socket] if socket else [])]
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
            new = ["systemd-run", "--user", "--scope", "--quiet", "--unit=claude-tmux", *new]
        run(new)
        return ran
    windows = _run([*tmux, "list-windows", "-t", f"={session}", "-F", "#{window_name}"])
    if window not in windows.stdout.split():
        run([*tmux, "new-window", "-d", "-t", f"={session}:", "-n", window, shell_command])
    return ran
