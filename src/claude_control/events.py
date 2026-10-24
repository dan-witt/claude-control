"""Hook events as the state machine sees them."""

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from . import store

TASK_NOTIFICATION = "task-notification"


@dataclass(frozen=True)
class TaskNotice:
    task_id: str
    tool_use_id: str | None
    status: str | None  # None while the task keeps running (Monitor events)


@dataclass(frozen=True)
class Event:
    received_at: float
    name: str
    session_id: str
    payload: dict[str, Any] = field(repr=False)
    pane: str | None = None
    socket: str | None = None
    pid: int | None = None

    @property
    def agent_id(self) -> str | None:
        return self.payload.get("agent_id")

    @property
    def tool_name(self) -> str | None:
        return self.payload.get("tool_name")

    @property
    def tool_use_id(self) -> str | None:
        return self.payload.get("tool_use_id")

    @classmethod
    def parse(
        cls, received_at: float, payload: dict[str, Any], headers: dict[str, str]
    ) -> "Event | None":
        name = payload.get("hook_event_name")
        session_id = payload.get("session_id")
        if not isinstance(name, str) or not isinstance(session_id, str):
            return None
        pid = headers.get("x-claude-pid") or ""
        return cls(
            received_at=received_at,
            name=name,
            session_id=session_id,
            payload=payload,
            pane=headers.get("x-tmux-pane") or None,
            socket=headers.get("x-claude-socket") or None,
            pid=int(pid) if pid.isdigit() else None,
        )

    @classmethod
    def from_stored(cls, e: store.Event) -> "Event | None":
        try:
            payload = json.loads(e.payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return cls.parse(e.received_at, payload, e.headers)


class _NoticeParser(HTMLParser):
    """Collects the child elements of each <task-notification>.

    html.parser is used instead of an XML parser because the notification body is free text
    (summaries, monitor output) that is not escaped.
    """

    FIELDS = ("task-id", "tool-use-id", "status")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.notices: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == TASK_NOTIFICATION:
            self._current = {}
        elif self._current is not None and tag in self.FIELDS:
            self._field = tag
            self._current[tag] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == TASK_NOTIFICATION and self._current is not None:
            self.notices.append(self._current)
            self._current = None
        if tag == self._field:
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._field is not None:
            self._current[self._field] += data


def task_notices(prompt: str) -> list[TaskNotice]:
    """Task notifications in a UserPromptSubmit prompt; empty for a prompt the user typed."""
    if not prompt.lstrip().startswith(f"<{TASK_NOTIFICATION}>"):
        return []
    parser = _NoticeParser()
    parser.feed(prompt)
    parser.close()
    return [
        TaskNotice(
            task_id=n["task-id"].strip(),
            tool_use_id=(n.get("tool-use-id") or "").strip() or None,
            status=(n.get("status") or "").strip() or None,
        )
        for n in parser.notices
        if n.get("task-id", "").strip()
    ]


def launched_task(payload: dict[str, Any]) -> tuple[str, str] | None:
    """(task id, kind) when a PostToolUse started a background task."""
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return None
    if response.get("status") == "async_launched" and response.get("agentId"):
        return str(response["agentId"]), "agent"
    if response.get("backgroundTaskId"):
        return str(response["backgroundTaskId"]), "shell"
    if payload.get("tool_name") == "Monitor" and response.get("taskId"):
        return str(response["taskId"]), "monitor"
    return None


def stopped_task(payload: dict[str, Any]) -> str | None:
    """Task id stopped by a TaskStop PostToolUse."""
    response = payload.get("tool_response")
    if payload.get("tool_name") == "TaskStop" and isinstance(response, dict):
        task_id = response.get("task_id")
        return str(task_id) if task_id else None
    return None
