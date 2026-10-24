"""Session list TUI. Talks to the daemon only over MCP."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar, Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Footer, Input

from .mcp_server import SESSIONS_URI, STATES

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE_FOR_S = 3.0
POLL_S = 1.0
RETRY_S = 3.0
NAME_WIDTH = 30

STATE_STYLE = {
    ("waiting", "permission"): "bold black on yellow",
    ("waiting", "input"): "bold black on yellow",
    ("waiting", "done"): "green",
    ("running", None): "cyan",
    ("error", None): "bold white on red",
    ("idle", None): "dim",
    ("ended", None): "dim",
}

WORDS = {
    "prompt": "got a prompt",
    "task notification": "background task reported",
    "discovered": "found running",
    "Stop": "finished its turn",
    "StopFailure": "turn failed",
    "Notification idle_prompt": "waiting for input",
    "Notification permission_prompt": "asking permission",
    "SessionStart startup": "started",
    "SessionStart resume": "resumed",
    "SessionStart clear": "cleared",
    "SessionStart compact": "compacted",
    "SessionStart fork": "forked",
}


def describe(last_event: str) -> str:
    """A session's last event in words."""
    if last_event in WORDS:
        return WORDS[last_event]
    name, _, detail = last_event.partition(" ")
    if name == "PreToolUse":
        return f"running {detail}"
    if name in ("PostToolUse", "PostToolUseFailure"):
        return f"ran {detail}" + (" (failed)" if name == "PostToolUseFailure" else "")
    if name == "PermissionRequest":
        return "asking a question" if detail == "AskUserQuestion" else f"asking to use {detail}"
    if name == "StopFailure":
        return f"turn failed ({detail})"
    if name == "SessionEnd":
        return f"ended ({detail})" if detail else "ended"
    return last_event


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def short_cwd(cwd: str | None, home: Path | None = None, width: int = 32) -> str:
    if not cwd:
        return ""
    home_str = str(home or Path.home())
    if cwd == home_str or cwd.startswith(home_str + "/"):
        cwd = "~" + cwd[len(home_str) :]
    if len(cwd) <= width:
        return cwd
    tail = "…/" + "/".join(cwd.rsplit("/", 2)[-2:])
    return tail if len(tail) <= width else "…" + cwd[-(width - 1) :]


def age(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def badge(state: str, reason: str | None) -> Text:
    label = f"{state}/{reason}" if reason else state
    style = STATE_STYLE.get((state, reason)) or STATE_STYLE.get((state, None), "")
    return Text(f" {label} ", style=style)


class FocusFailed(Exception):
    pass


class Backend(Protocol):
    async def sessions(self, include_ended: bool) -> list[dict[str, Any]]: ...

    async def focus(self, session_id: str) -> None: ...


# Opens a backend; the callable it is given is invoked whenever the daemon reports a change.
BackendFactory = Callable[[Callable[[], None]], contextlib.AbstractAsyncContextManager[Backend]]


class McpBackend:
    def __init__(self, client: Client):
        self.client = client

    async def sessions(self, include_ended: bool) -> list[dict[str, Any]]:
        args = {"states": list(STATES)} if include_ended else {}
        result = await self.client.call_tool("list_sessions", args)
        if result.is_error:
            raise RuntimeError(_text(result))
        data = result.structured_content
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        return data if isinstance(data, list) else []

    async def focus(self, session_id: str) -> None:
        result = await self.client.call_tool("focus_session", {"session_id": session_id})
        if result.is_error:
            raise FocusFailed(_text(result))


def _text(result: Any) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


def mcp_backend(url: str, token: str) -> BackendFactory:
    @contextlib.asynccontextmanager
    async def connect(on_change: Callable[[], None]) -> AsyncIterator[Backend]:
        headers = {"authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            Client(streamable_http_client(url, http_client=http)) as client,  # type: ignore[arg-type]
            client.listen(resource_subscriptions=[SESSIONS_URI]) as sub,
        ):

            async def forward() -> None:
                async for _ in sub:
                    on_change()

            listener = asyncio.create_task(forward())
            try:
                yield McpBackend(client)
            finally:
                listener.cancel()

    return connect


class FilterInput(Input):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "app.clear_filter", "clear filter")]


class SessionsApp(App):
    TITLE = "claude-control"
    CSS = """
    #filter { display: none; }
    #filter.shown { display: block; }
    DataTable { height: 1fr; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        # priority: the table would otherwise take Enter and the footer would not show it
        Binding("enter", "focus_session", "focus", priority=True),
        Binding("slash", "show_filter", "filter"),
        Binding("e", "toggle_ended", "ended"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, backend: BackendFactory, poll_s: float = POLL_S):
        super().__init__()
        self.connect = backend
        self.poll_s = poll_s
        self.backend: Backend | None = None
        self.rows: list[dict[str, Any]] = []
        self.fetched_at = 0.0
        self.include_ended = False
        self.filter = ""
        self.disconnected = False
        self.wake = asyncio.Event()
        # session_id -> (tool_count, time it last changed)
        self.activity: dict[str, tuple[int, float]] = {}

    def compose(self) -> ComposeResult:
        yield FilterInput(placeholder="filter: name, directory or state", id="filter")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column(" ", key="act", width=1)
        table.add_column("session", key="name")
        table.add_column("directory", key="cwd")
        table.add_column("state", key="state")
        table.add_column("last event", key="event")
        table.add_column("age", key="age", width=4)
        table.focus()
        self.set_interval(0.15, self.tick)
        self.run_worker(self.sync(), exclusive=True, exit_on_error=False)

    # --- data

    async def sync(self) -> None:
        while True:
            try:
                async with self.connect(self.wake.set) as backend:
                    self.backend = backend
                    while True:
                        rows = await backend.sessions(self.include_ended)
                        if self.disconnected:
                            self.disconnected = False
                            self.notify("reconnected to the daemon")
                        self.update_rows(rows)
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self.wake.wait(), self.poll_s)
                        self.wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure here means reconnect
                self.backend = None
                if not self.disconnected:
                    self.disconnected = True
                    self.notify(
                        f"daemon not reachable ({type(e).__name__}); retrying",
                        severity="error",
                        timeout=10,
                    )
                await asyncio.sleep(RETRY_S)

    def update_rows(self, rows: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        for r in rows:
            count = r.get("tool_count") or 0
            previous = self.activity.get(r["session_id"])
            if previous is None:
                self.activity[r["session_id"]] = (count, 0.0)
            elif previous[0] != count:
                self.activity[r["session_id"]] = (count, now)
        self.rows = rows
        self.fetched_at = now
        self.render_rows()

    def visible_rows(self) -> list[dict[str, Any]]:
        needle = self.filter.lower()
        if not needle:
            return self.rows
        return [
            r
            for r in self.rows
            if any(
                needle in str(r.get(k) or "").lower()
                for k in ("name", "cwd", "state", "reason", "session_id")
            )
        ]

    def is_active(self, r: dict[str, Any], now: float) -> bool:
        if r.get("current_tool"):
            return True
        changed_at = self.activity.get(r["session_id"], (0, 0.0))[1]
        return now - changed_at < ACTIVE_FOR_S

    def render_rows(self) -> None:
        table = self.query_one(DataTable)
        selected = self.selected_id()
        table.clear()
        now = time.monotonic()
        for r in self.visible_rows():
            table.add_row(
                self.spinner(r, now),
                clip(r.get("name") or r["session_id"][:8], NAME_WIDTH),
                short_cwd(r.get("cwd")),
                badge(r["state"], r.get("reason")),
                describe(r.get("last_event") or ""),
                age(r.get("last_event_age_s", 0)),
                key=r["session_id"],
            )
        if selected is not None:
            with contextlib.suppress(Exception):
                table.move_cursor(row=table.get_row_index(selected))

    def spinner(self, r: dict[str, Any], now: float) -> str:
        if r["state"] == "ended" or not self.is_active(r, now):
            return ""
        return SPINNER[int(now * 8) % len(SPINNER)]

    def tick(self) -> None:
        table = self.query_one(DataTable)
        now = time.monotonic()
        since_fetch = now - self.fetched_at
        for r in self.visible_rows():
            key = r["session_id"]
            with contextlib.suppress(Exception):
                table.update_cell(key, "act", self.spinner(r, now))
                table.update_cell(key, "age", age(r.get("last_event_age_s", 0) + since_fetch))

    def selected_id(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key.value is not None else None

    # --- actions

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_focus_session(self) -> None:
        box = self.query_one("#filter", Input)
        if box.has_focus:
            if not self.filter:
                box.remove_class("shown")
            self.query_one(DataTable).focus()
            return
        session_id = self.selected_id()
        if session_id is None:
            return
        if self.backend is None:
            self.notify("not connected to the daemon", severity="error")
            return
        self.run_worker(self.focus(self.backend, session_id), group="focus")

    async def focus(self, backend: Backend, session_id: str) -> None:
        try:
            await backend.focus(session_id)
        except FocusFailed as e:
            self.notify(str(e), severity="error")

    def action_toggle_ended(self) -> None:
        self.include_ended = not self.include_ended
        self.wake.set()

    def action_show_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("shown")
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.value = ""
        self.filter = ""
        box.remove_class("shown")
        self.query_one(DataTable).focus()
        self.render_rows()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter = event.value
        self.render_rows()
