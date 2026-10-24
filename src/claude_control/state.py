"""Session state from hook events and `claude agents` reconciliation. No I/O."""

from dataclasses import dataclass, field
from typing import Any

from .events import Event, launched_task, stopped_task, task_notices

IDLE_AFTER_S = 600
# Reconciliation acts only on sessions whose last event is older than this: `claude agents`
# can lag hooks, and a new process can post SessionStart before it is listed.
STALE_AFTER_S = 15

TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"}


@dataclass
class Process:
    socket: str | None
    pid: int | None
    pane: str | None
    first_seen: float
    listed: bool = False  # seen in `claude agents`
    last_event_at: float = 0.0


@dataclass
class Task:
    task_id: str
    kind: str  # agent | shell | monitor
    opened_at: float
    last_event_at: float
    agent_type: str | None = None


@dataclass
class Agent:
    """One row of `claude agents --json`."""

    pid: int
    session_id: str
    status: str
    cwd: str | None = None
    kind: str | None = None
    name: str | None = None

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> "Agent | None":
        if not isinstance(row.get("pid"), int) or not isinstance(row.get("sessionId"), str):
            return None
        return cls(
            pid=row["pid"],
            session_id=row["sessionId"],
            status=str(row.get("status", "")),
            cwd=row.get("cwd"),
            kind=row.get("kind"),
            name=row.get("name"),
        )


@dataclass
class Session:
    session_id: str
    state: str
    started_at: float
    last_event_at: float
    reason: str | None = None
    last_event: str = ""
    cwd: str | None = None
    name: str | None = None
    kind: str | None = None
    message: str | None = None
    processes: list[Process] = field(default_factory=list)
    open_tools: dict[str, str] = field(default_factory=dict)  # tool_use_id -> tool name
    tool_count: int = 0
    permission_tool: str | None = None
    tasks: dict[str, Task] = field(default_factory=dict)
    ended_at: float | None = None

    @property
    def current_tool(self) -> str | None:
        return next(reversed(self.open_tools.values()), None)

    @property
    def focus_pane(self) -> str | None:
        """Pane of the process with the most recent hook event."""
        panes = [p for p in self.processes if p.pane]
        return max(panes, key=lambda p: p.last_event_at).pane if panes else None

    def set(self, state: str, reason: str | None = None) -> None:
        self.state, self.reason = state, reason

    def end_turn(self) -> None:
        self.open_tools.clear()
        self.permission_tool = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "reason": self.reason,
            "name": self.name,
            "cwd": self.cwd,
            "kind": self.kind,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "last_event": self.last_event,
            "message": self.message,
            "current_tool": self.current_tool,
            "tool_count": self.tool_count,
            "processes": [
                {"pid": p.pid, "pane": p.pane, "socket": p.socket} for p in self.processes
            ],
            "tasks": [
                {
                    "task_id": t.task_id,
                    "kind": t.kind,
                    "agent_type": t.agent_type,
                    "opened_at": t.opened_at,
                    "last_event_at": t.last_event_at,
                }
                for t in self.tasks.values()
            ],
            "ended_at": self.ended_at,
        }


def _describe(e: Event) -> str:
    p = e.payload
    if e.name == "UserPromptSubmit":
        return "task notification" if task_notices(p.get("prompt") or "") else "prompt"
    detail = e.tool_name or p.get("notification_type") or p.get("source") or p.get("reason") or ""
    return f"{e.name} {detail}".strip()


class Tracker:
    def __init__(self, idle_after: float = IDLE_AFTER_S, stale_after: float = STALE_AFTER_S):
        self.idle_after = idle_after
        self.stale_after = stale_after
        self.sessions: dict[str, Session] = {}

    # --- hook events

    def apply(self, e: Event) -> Session | None:
        agent = e.agent_id is not None and e.name != "SessionStart"
        handler = getattr(self, f"_on_{e.name}", None)
        if not agent and handler is None:
            return self.sessions.get(e.session_id)
        s = self.sessions.get(e.session_id)
        if s is None:
            # First event seen mid-session (daemon started later): assume a turn is running
            # unless the event itself says otherwise.
            s = Session(e.session_id, "running", started_at=e.received_at, last_event_at=0)
            if e.name == "SessionStart":
                s.set("idle")
            self.sessions[e.session_id] = s
        if agent:
            self._agent_event(s, e)
            return s
        if e.name != "SessionEnd":
            self._attach(s, e)
        if e.payload.get("cwd"):
            s.cwd = e.payload["cwd"]
        assert handler is not None
        handler(s, e)
        s.last_event_at = e.received_at
        s.last_event = _describe(e)
        return s

    def _agent_event(self, s: Session, e: Event) -> None:
        agent_id = e.agent_id
        assert agent_id is not None
        if e.name == "SubagentStop":
            s.tasks.pop(agent_id, None)
            return
        if e.name not in TOOL_EVENTS:
            return
        task = s.tasks.get(agent_id)
        if task is None:
            task = Task(agent_id, "agent", e.received_at, e.received_at)
            s.tasks[agent_id] = task
        task.agent_type = e.payload.get("agent_type") or task.agent_type
        task.last_event_at = e.received_at

    def _find_process(self, s: Session, e: Event) -> Process | None:
        if e.socket:
            for p in s.processes:
                if p.socket == e.socket:
                    return p
        if e.pid:
            for p in s.processes:
                if p.pid == e.pid:
                    return p
        if e.pane:
            for p in s.processes:
                if p.pane == e.pane and (p.socket is None or e.socket is None):
                    return p
        if not e.socket and len(s.processes) == 1:
            return s.processes[0]
        if e.socket:
            # A process known only by pid (reconciliation) or by nothing (events from before
            # the socket header) is the sender if it is the only one without a socket.
            unknown = [p for p in s.processes if p.socket is None]
            if len(unknown) == 1:
                return unknown[0]
        return None

    def _attach(self, s: Session, e: Event) -> None:
        """Record the process that sent `e`, moving it from another session if needed."""
        p = self._find_process(s, e)
        if p is None:
            reason = e.payload.get("source") or "moved"
            p = self._take(s, e.socket, e.pid, reason, e.received_at)
            p = p or Process(e.socket, e.pid, e.pane, e.received_at)
            s.processes.append(p)
        p.socket = p.socket or e.socket
        p.pid = p.pid or e.pid
        p.pane = p.pane or e.pane
        p.last_event_at = e.received_at

    def _take(
        self, s: Session, socket: str | None, pid: int | None, reason: str, at: float
    ) -> Process | None:
        """Detach the process with this socket or pid from any other session.

        A process changes session on /clear and /resume; normally the old session has already
        ended by SessionEnd, so this matters only when that event was lost.
        """
        if not socket and not pid:
            return None
        for other in self.sessions.values():
            if other is s:
                continue
            for p in other.processes:
                if (socket and p.socket == socket) or (pid and p.pid == pid):
                    other.processes.remove(p)
                    if not other.processes and other.state != "ended":
                        self._end(other, reason, at)
                    return p
        return None

    def _end(self, s: Session, reason: str | None, at: float) -> None:
        s.set("ended", reason)
        s.end_turn()
        s.tasks.clear()
        s.ended_at = at

    def _on_SessionStart(self, s: Session, e: Event) -> None:
        # New sessions start idle; compact and a second resume of a live session keep state.
        if s.state == "ended":
            s.ended_at = None
            s.set("idle")

    def _on_UserPromptSubmit(self, s: Session, e: Event) -> None:
        for notice in task_notices(e.payload.get("prompt") or ""):
            if notice.status is not None:
                s.tasks.pop(notice.task_id, None)
        s.permission_tool = None
        s.set("running")

    def _on_PreToolUse(self, s: Session, e: Event) -> None:
        if e.tool_use_id:
            s.open_tools[e.tool_use_id] = e.tool_name or ""
        if s.state != "running":
            s.permission_tool = None
            s.set("running")

    def _on_PermissionRequest(self, s: Session, e: Event) -> None:
        s.permission_tool = e.tool_name
        s.set("waiting", "input" if e.tool_name == "AskUserQuestion" else "permission")

    def _on_PostToolUse(self, s: Session, e: Event) -> None:
        if e.tool_use_id:
            s.open_tools.pop(e.tool_use_id, None)
        s.tool_count += 1
        launched = launched_task(e.payload)
        if launched:
            task_id, kind = launched
            s.tasks[task_id] = Task(task_id, kind, e.received_at, e.received_at)
        stopped = stopped_task(e.payload)
        if stopped:
            s.tasks.pop(stopped, None)
        if s.state != "running":
            s.permission_tool = None
            s.set("running")

    _on_PostToolUseFailure = _on_PostToolUse

    def _on_Notification(self, s: Session, e: Event) -> None:
        kind = e.payload.get("notification_type")
        if e.payload.get("message"):
            s.message = e.payload["message"]
        if kind == "idle_prompt":
            # Not sent while a prompt is open; after a turn that ended without Stop
            # (interrupt) or a dismissed prompt, it is the only hook.
            if s.state == "running" or (s.state == "waiting" and s.reason != "done"):
                s.end_turn()
                s.set("waiting", "done")
        elif kind in ("elicitation_dialog", "agent_needs_input"):
            s.set("waiting", "input")

    def _on_Stop(self, s: Session, e: Event) -> None:
        s.end_turn()
        s.message = e.payload.get("last_assistant_message") or s.message
        s.set("waiting", "done")

    def _on_StopFailure(self, s: Session, e: Event) -> None:
        s.end_turn()
        s.message = e.payload.get("last_assistant_message") or s.message
        s.set("error", e.payload.get("error") or None)

    def _on_SessionEnd(self, s: Session, e: Event) -> None:
        p = self._find_process(s, e)
        if p is not None:
            s.processes.remove(p)
        if not s.processes and s.state != "ended":
            self._end(s, e.payload.get("reason"), e.received_at)

    def signature(self) -> tuple:
        """Changes when a session is added or removed, or its state or processes change.

        Tool activity does not change it; resource subscribers are notified on changes.
        """
        return tuple(
            sorted(
                (
                    s.session_id,
                    s.state,
                    s.reason,
                    s.name,
                    tuple(sorted((p.pid or 0, p.pane or "") for p in s.processes)),
                )
                for s in self.sessions.values()
            )
        )

    def forget_ended(self, ended_before: float) -> None:
        for sid in [
            sid
            for sid, s in self.sessions.items()
            if s.state == "ended" and s.ended_at is not None and s.ended_at < ended_before
        ]:
            del self.sessions[sid]

    # --- time

    def tick(self, now: float) -> None:
        for s in self.sessions.values():
            if (
                s.state == "waiting"
                and s.reason == "done"
                and not s.tasks
                and now - s.last_event_at >= self.idle_after
            ):
                s.set("idle")

    # --- reconciliation

    def reconcile(self, agents: list[Agent], panes: dict[int, str], now: float) -> None:
        """Apply one `claude agents --json` listing; `panes` maps pid -> tmux pane."""
        by_session: dict[str, list[Agent]] = {}
        for a in agents:
            by_session.setdefault(a.session_id, []).append(a)
        listed = {a.pid for a in agents}

        for a in agents:
            self._reconcile_process(a, panes.get(a.pid), now)

        for s in self.sessions.values():
            if s.state == "ended":
                continue
            for p in list(s.processes):
                gone = (p.pid is not None and p.pid not in listed) or (
                    p.pid is None and s.session_id not in by_session
                )
                if gone and (p.listed or now - p.first_seen >= self.stale_after):
                    s.processes.remove(p)
            if not s.processes:
                self._end(s, "process_gone", now)
                continue
            rows = by_session.get(s.session_id)
            if rows and now - s.last_event_at >= self.stale_after:
                self._reconcile_status(s, {a.status for a in rows}, now)

    def _reconcile_process(self, a: Agent, pane: str | None, now: float) -> None:
        s = self.sessions.get(a.session_id)
        if s is None:
            s = Session(a.session_id, "idle", started_at=now, last_event_at=now)
            s.last_event = "discovered"
            if a.status == "busy":
                s.set("running")
            elif a.status == "waiting":
                s.set("waiting", "permission")
            self.sessions[a.session_id] = s
        elif s.state == "ended":
            s.ended_at = None
            s.set("idle")
        s.name, s.kind = a.name or s.name, a.kind or s.kind
        s.cwd = s.cwd or a.cwd

        p = next((p for p in s.processes if p.pid == a.pid), None)
        if p is None:
            p = next((p for p in s.processes if p.pid is None), None)
        if p is None:
            p = self._take(s, None, a.pid, "moved", now) or Process(None, a.pid, pane, now)
            s.processes.append(p)
        p.pid = a.pid
        p.pane = p.pane or pane
        p.listed = True

    def _reconcile_status(self, s: Session, statuses: set[str], now: float) -> None:
        if "busy" in statuses:
            return
        if "waiting" in statuses:
            if s.state == "running":
                s.set("waiting", "permission")
            return
        # Every process idle: no turn, no prompt on screen and no background work.
        s.tasks = {k: t for k, t in s.tasks.items() if now - t.last_event_at < self.stale_after}
        if s.state == "running" or (s.state == "waiting" and s.reason != "done"):
            s.end_turn()
            s.set("waiting", "done")
