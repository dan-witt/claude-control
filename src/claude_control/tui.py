"""Session list TUI. Talks to the daemon only over MCP."""

import asyncio
import contextlib
import os
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
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import DataTable, Footer, Input, Label, TextArea

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


class LaunchFailed(Exception):
    pass


class Backend(Protocol):
    async def sessions(self, include_ended: bool) -> list[dict[str, Any]]: ...

    async def focus(self, session_id: str) -> None: ...

    async def launch(self, cwd: str, prompt: str | None, name: str | None) -> dict[str, Any]: ...


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

    async def launch(self, cwd: str, prompt: str | None, name: str | None) -> dict[str, Any]:
        args: dict[str, Any] = {"cwd": cwd}
        if prompt:
            args["prompt"] = prompt
        if name:
            args["name"] = name
        result = await self.client.call_tool("launch_session", args)
        if result.is_error:
            raise LaunchFailed(_text(result))
        data = result.structured_content
        return data if isinstance(data, dict) else {}


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


class PathSuggester(Suggester):
    """Completes the next directory component of a path."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        expanded = os.path.expanduser(value)
        head, tail = os.path.split(expanded)
        try:
            names = sorted(os.listdir(head or "."))
        except OSError:
            return None
        for name in names:
            hidden = name.startswith(".") and not tail.startswith(".")
            if name.startswith(tail) and not hidden and os.path.isdir(os.path.join(head, name)):
                completed = os.path.join(head, name) + "/"
                return value + completed[len(expanded) :] if completed != expanded else None
        return None


class LaunchScreen(ModalScreen[dict[str, Any] | None]):
    """New session: directory, optional name, optional multi-line prompt."""

    CSS = """
    LaunchScreen { align: center middle; }
    #dialog { width: 90; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #prompt { height: 8; }
    #error { color: $error; height: auto; }
    Label { margin-top: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "launch", "launch", priority=True),
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, backend: Backend, cwd: str):
        super().__init__()
        self.backend = backend
        self.default_cwd = cwd

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("New session — ctrl+s launches, esc cancels")
            yield Label("directory  (→ accepts a completion)")
            yield Input(self.default_cwd, id="cwd", suggester=PathSuggester())
            yield Label("name  (optional)")
            yield Input(placeholder="directory name plus two hex digits", id="name")
            yield Label("prompt  (optional)")
            yield TextArea(id="prompt", tab_behavior="focus")
            yield Label("", id="error")
            yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cwd", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.focus_next()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def action_launch(self) -> None:
        cwd = self.query_one("#cwd", Input).value.strip()
        name = self.query_one("#name", Input).value.strip() or None
        prompt = self.query_one("#prompt", TextArea).text.strip() or None
        error = self.query_one("#error", Label)
        if not cwd:
            error.update("a directory is required")
            return
        try:
            result = await self.backend.launch(cwd, prompt, name)
        except LaunchFailed as e:
            error.update(str(e))
            return
        self.dismiss(result)


class SessionTable(DataTable):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "app.focus_session", "focus")]


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
        Binding("slash", "show_filter", "filter"),
        Binding("n", "new_session", "new"),
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
        self.table = SessionTable(cursor_type="row", zebra_stripes=True)
        self.filter_box = FilterInput(placeholder="filter: name, directory or state", id="filter")
        self.wake = asyncio.Event()
        # session_id -> (tool_count, time it last changed)
        self.activity: dict[str, tuple[int, float]] = {}

    def compose(self) -> ComposeResult:
        yield self.filter_box
        yield self.table
        yield Footer()

    def on_mount(self) -> None:
        table = self.table
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
        table = self.table
        selected = self.selected_id()
        table.clear()
        now = time.monotonic()
        for r in self.visible_rows():
            table.add_row(
                self.spinner(r, now),
                clip(r.get("name") or r["session_id"][:8], NAME_WIDTH),
                short_cwd(r.get("cwd")),
                badge(r["state"], r.get("reason")),
                r.get("last_event_text") or "",
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
        if not self.table.is_mounted:
            return
        table = self.table
        now = time.monotonic()
        since_fetch = now - self.fetched_at
        for r in self.visible_rows():
            key = r["session_id"]
            with contextlib.suppress(Exception):
                table.update_cell(key, "act", self.spinner(r, now))
                table.update_cell(key, "age", age(r.get("last_event_age_s", 0) + since_fetch))

    def selected_id(self) -> str | None:
        table = self.table
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key.value is not None else None

    # --- actions

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()

    def action_focus_session(self) -> None:
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

    def action_new_session(self) -> None:
        if self.backend is None:
            self.notify("not connected to the daemon", severity="error")
            return
        selected = next((r for r in self.rows if r["session_id"] == self.selected_id()), None)
        cwd = (selected or {}).get("cwd") or os.path.expanduser("~")

        def launched(result: dict[str, Any] | None) -> None:
            if result:
                self.notify(f"launched {result.get('name')}")
                self.wake.set()

        self.push_screen(LaunchScreen(self.backend, cwd), launched)

    def action_toggle_ended(self) -> None:
        self.include_ended = not self.include_ended
        self.wake.set()

    def action_show_filter(self) -> None:
        box = self.filter_box
        box.add_class("shown")
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.filter_box
        box.value = ""
        self.filter = ""
        box.remove_class("shown")
        self.table.focus()
        self.render_rows()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is not self.filter_box:
            return
        self.filter = event.value
        self.render_rows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is not self.filter_box:
            return
        if not self.filter:
            self.filter_box.remove_class("shown")
        self.table.focus()
