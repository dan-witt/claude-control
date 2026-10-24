"""tmux commands the daemon runs. None of them starts a tmux server."""

import asyncio

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
