"""Inputs for Tracker.reconcile: `claude agents --json` and tmux panes."""

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from .state import Agent

log = logging.getLogger(__name__)

TIMEOUT_S = 10


async def _run(*argv: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    assert proc.returncode is not None
    return proc.returncode, out, err


async def list_agents(claude_bin: str) -> list[Agent] | None:
    """Running Claude Code processes, or None when the listing failed (reconcile nothing)."""
    try:
        code, out, err = await _run(claude_bin, "agents", "--json")
    except (OSError, TimeoutError) as e:
        log.warning("claude agents failed: %s", e)
        return None
    if code != 0:
        log.warning("claude agents exited %d: %s", code, err.decode(errors="replace").strip())
        return None
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as e:
        log.warning("claude agents output is not JSON: %s", e)
        return None
    if not isinstance(rows, list):
        return None
    return [a for a in (Agent.from_json(r) for r in rows if isinstance(r, dict)) if a]


async def pane_pids() -> dict[int, str]:
    """pane_pid -> pane id for every pane of the default tmux server; empty if none runs.

    `list-panes` does not start a server.
    """
    try:
        code, out, _ = await _run("tmux", "list-panes", "-a", "-F", "#{pane_pid} #{pane_id}")
    except (OSError, TimeoutError):
        return {}
    if code != 0:
        return {}
    panes = {}
    for line in out.decode().splitlines():
        pid, _, pane = line.partition(" ")
        if pid.isdigit() and pane:
            panes[int(pid)] = pane
    return panes


def parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 2 (comm) is in parentheses and may contain spaces; ppid is the 2nd field after it.
    fields = stat.rpartition(")")[2].split()
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None


def panes_for(
    pids: Iterable[int],
    pane_by_pid: dict[int, str],
    parent: Callable[[int], int | None] = parent_pid,
) -> dict[int, str]:
    """Map each pid to the pane whose process is the pid or one of its ancestors."""
    result = {}
    for pid in pids:
        current: int | None = pid
        while current is not None and current > 1:
            if current in pane_by_pid:
                result[pid] = pane_by_pid[current]
                break
            current = parent(current)
    return result
