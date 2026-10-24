"""MCP tools and resource over the tracker. Names and fields are a contract (DESIGN.md)."""

import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.subscriptions import SubscriptionBus
from mcp.types import ToolAnnotations

from . import tmux
from .state import Session, Tracker

SESSIONS_URI = "sessions://"
STATES = ("starting", "running", "waiting", "idle", "error", "ended")

INSTRUCTIONS = """\
Tracks the Claude Code sessions running on this machine.
States: running (working), waiting (reason permission / input / done: needs the user or has
finished its turn), idle (done and untouched for 10 minutes, or at an empty prompt),
error (the turn failed), ended.
A session is identified by its session_id, a unique prefix of it, or its name."""

Focus = Callable[[str], Awaitable[str]]


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


def summary(s: Session, now: float) -> dict[str, Any]:
    return {
        "session_id": s.session_id,
        "name": s.name,
        "state": s.state,
        "reason": s.reason,
        "cwd": s.cwd,
        "last_event": s.last_event,
        "last_event_age_s": round(now - s.last_event_at),
        "started_at": _iso(s.started_at),
        "pane": s.focus_pane,
        "current_tool": s.current_tool,
        "tool_count": s.tool_count,
        "background_tasks": len(s.tasks),
    }


def detail(s: Session, now: float) -> dict[str, Any]:
    return {
        **summary(s, now),
        "message": s.message,
        "permission_tool": s.permission_tool,
        "processes": [{"pid": p.pid, "pane": p.pane} for p in s.processes],
        "tasks": [
            {
                "task_id": t.task_id,
                "kind": t.kind,
                "agent_type": t.agent_type,
                "last_event_age_s": round(now - t.last_event_at),
            }
            for t in s.tasks.values()
        ],
        "ended_at": _iso(s.ended_at),
    }


def resolve(tracker: Tracker, ref: str) -> Session:
    """Session by id, name, or unique id prefix."""
    if ref in tracker.sessions:
        return tracker.sessions[ref]
    by_name = [s for s in tracker.sessions.values() if s.name == ref and s.state != "ended"]
    if len(by_name) == 1:
        return by_name[0]
    by_prefix = [s for s in tracker.sessions.values() if s.session_id.startswith(ref)]
    if len(ref) >= 4 and len(by_prefix) == 1:
        return by_prefix[0]
    if len(by_name) > 1 or len(by_prefix) > 1:
        raise ToolError(f"{ref!r} matches more than one session")
    raise ToolError(f"no session {ref!r}")


def live_sessions(tracker: Tracker, states: list[str] | None = None) -> list[Session]:
    wanted = set(states or ())
    unknown = wanted - set(STATES)
    if unknown:
        raise ToolError(f"unknown state(s) {sorted(unknown)}; states are {list(STATES)}")
    if wanted:
        rows = [s for s in tracker.sessions.values() if s.state in wanted]
    else:
        rows = [s for s in tracker.sessions.values() if s.state != "ended"]
    return sorted(rows, key=lambda s: s.started_at)


def build(tracker: Tracker, bus: SubscriptionBus, focus: Focus = tmux.focus) -> MCPServer:
    mcp = MCPServer(name="claude-control", instructions=INSTRUCTIONS, subscriptions=bus)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @mcp.tool(annotations=read_only)
    def list_sessions(states: list[str] | None = None) -> list[dict[str, Any]]:
        """List Claude Code sessions, oldest first.

        `states` filters by state (running, waiting, idle, error, ended); without it, every
        session that has not ended. Sessions waiting on the user: states=["waiting"] and
        reason permission or input.
        """
        now = time.time()
        return [summary(s, now) for s in live_sessions(tracker, states)]

    @mcp.tool(annotations=read_only)
    def get_session(session_id: str) -> dict[str, Any]:
        """One session in detail: its last assistant message or notification text, processes
        and panes, tool activity and open background tasks (forks, background shells)."""
        return detail(resolve(tracker, session_id), time.time())

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False))
    async def focus_session(session_id: str) -> dict[str, Any]:
        """Switch the most recently used tmux client to the session's pane."""
        s = resolve(tracker, session_id)
        if s.state == "ended":
            raise ToolError(f"session {s.name or s.session_id} has ended")
        pane = s.focus_pane
        if pane is None:
            raise ToolError(f"session {s.name or s.session_id} is not running in tmux")
        try:
            client = await focus(pane)
        except tmux.TmuxError as e:
            raise ToolError(str(e)) from e
        return {"session_id": s.session_id, "pane": pane, "client": client}

    @mcp.resource(
        SESSIONS_URI,
        name="sessions",
        mime_type="application/json",
        description="Every session that has not ended, as list_sessions returns it. "
        "Updated when a session starts, ends, or changes state.",
    )
    def sessions_resource() -> str:
        now = time.time()
        return json.dumps([summary(s, now) for s in live_sessions(tracker)])

    return mcp
