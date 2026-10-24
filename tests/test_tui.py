import contextlib
import subprocess
from pathlib import Path

import pytest
from textual.widgets import DataTable

from claude_control import tmux
from claude_control.tui import FocusFailed, SessionsApp, age, describe, short_cwd


def row(sid, name, state, reason=None, **kw):
    return {
        "session_id": sid,
        "name": name,
        "state": state,
        "reason": reason,
        "cwd": f"/w/{name}",
        "last_event": "Stop",
        "last_event_age_s": 30,
        "current_tool": None,
        "tool_count": 0,
        **kw,
    }


class FakeBackend:
    def __init__(self, rows, ended=(), focus_error=None):
        self.rows = rows
        self.ended = list(ended)
        self.focus_error = focus_error
        self.focused: list[str] = []
        self.include_ended_calls: list[bool] = []
        self.on_change = None

    def factory(self):
        @contextlib.asynccontextmanager
        async def connect(on_change):
            self.on_change = on_change
            yield self

        return connect

    async def sessions(self, include_ended):
        self.include_ended_calls.append(include_ended)
        return self.rows + (self.ended if include_ended else [])

    async def focus(self, session_id):
        self.focused.append(session_id)
        if self.focus_error:
            raise FocusFailed(self.focus_error)


ROWS = [
    row("s1", "alpha", "running", current_tool="Bash"),
    row("s2", "beta", "waiting", "permission"),
    row("s3", "gamma", "idle"),
]


def keys(app):
    table = app.query_one(DataTable)
    return [str(k.value) for k in table.rows]


@pytest.mark.anyio
async def test_lists_sessions_in_given_order():
    backend = FakeBackend(ROWS)
    app = SessionsApp(backend.factory(), poll_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert keys(app) == ["s1", "s2", "s3"]


@pytest.mark.anyio
async def test_enter_focuses_selected_session():
    backend = FakeBackend(ROWS)
    app = SessionsApp(backend.factory(), poll_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("j", "j", "k", "enter")
        await pilot.pause(0.1)
    assert backend.focused == ["s2"]


@pytest.mark.anyio
async def test_cursor_stays_on_session_when_rows_change():
    backend = FakeBackend(ROWS)
    app = SessionsApp(backend.factory(), poll_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        await pilot.press("j", "j")
        assert app.selected_id() == "s3"
        backend.rows = [ROWS[0], ROWS[2]]  # s2 ended
        await pilot.pause(0.2)
        assert app.selected_id() == "s3"


@pytest.mark.anyio
async def test_focus_error_is_shown():
    backend = FakeBackend(ROWS, focus_error="session gamma is not running in tmux")
    app = SessionsApp(backend.factory(), poll_s=0.05)
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.1)
        messages = [n.message for n in app._notifications]
    assert messages == ["session gamma is not running in tmux"]


@pytest.mark.anyio
async def test_e_toggles_ended_sessions():
    backend = FakeBackend(ROWS, ended=[row("s0", "old", "ended", "other")])
    app = SessionsApp(backend.factory(), poll_s=5)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert "s0" not in keys(app)
        await pilot.press("e")
        await pilot.pause(0.1)
        assert "s0" in keys(app)
    assert backend.include_ended_calls[:2] == [False, True]


@pytest.mark.anyio
async def test_filter_narrows_and_escape_clears():
    backend = FakeBackend(ROWS)
    app = SessionsApp(backend.factory(), poll_s=5)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash", "b", "e", "t")
        assert keys(app) == ["s2"]
        await pilot.press("enter")  # keeps the filter, back to the table
        assert keys(app) == ["s2"]
        await pilot.press("slash", "escape")
        assert keys(app) == ["s1", "s2", "s3"]


@pytest.mark.anyio
async def test_change_notification_refreshes_immediately():
    backend = FakeBackend(ROWS[:1])
    app = SessionsApp(backend.factory(), poll_s=60)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        backend.rows = ROWS
        assert backend.on_change is not None
        backend.on_change()
        await pilot.pause(0.1)
        assert keys(app) == ["s1", "s2", "s3"]


@pytest.mark.anyio
async def test_unreachable_daemon_is_reported():
    @contextlib.asynccontextmanager
    async def broken(on_change):
        raise ConnectionError("refused")
        yield  # pragma: no cover

    app = SessionsApp(broken)
    async with app.run_test(notifications=True) as pilot:
        await pilot.pause(0.1)
        messages = [n.message for n in app._notifications]
    assert len(messages) == 1 and "daemon not reachable" in messages[0]


def test_activity_from_tool_count_changes():
    app = SessionsApp(FakeBackend([]).factory())
    first = row("s9", "x", "running", tool_count=4)
    app.activity["s9"] = (3, 0.0)
    assert app.is_active(first, now=100.0) is False  # count unchanged since record at t=0
    app.activity["s9"] = (4, 99.0)
    assert app.is_active(first, now=100.0) is True
    assert app.is_active(first, now=103.5) is False
    assert app.is_active(row("s9", "x", "running", current_tool="Bash"), now=200.0) is True


def test_describe_events():
    assert describe("PreToolUse Bash") == "running Bash"
    assert describe("PostToolUseFailure Read") == "ran Read (failed)"
    assert describe("PermissionRequest AskUserQuestion") == "asking a question"
    assert describe("PermissionRequest Bash") == "asking to use Bash"
    assert describe("Stop") == "finished its turn"
    assert describe("SessionEnd other") == "ended (other)"
    assert describe("task notification") == "background task reported"
    assert describe("Something New") == "Something New"


def test_short_cwd_and_age():
    home = Path("/home/u")
    assert short_cwd("/home/u/src/x", home) == "~/src/x"
    assert short_cwd("/home/u/" + "a" * 40 + "/proj/sub", home) == "…/proj/sub"
    long_tail = short_cwd("/tmp/x/" + "b" * 40 + "/scratchpad", home)
    assert len(long_tail) == 32 and long_tail.startswith("…") and long_tail.endswith("/scratchpad")
    assert short_cwd(None) == ""
    assert [age(s) for s in (5, 125, 7300, 200000)] == ["5s", "2m", "2h", "2d"]


# --- tmux bootstrap, on an isolated tmux server


@pytest.fixture
def tmux_socket():
    name = "cc-test-tui"
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, check=False)


def windows(socket, session):
    out = subprocess.run(
        ["tmux", "-L", socket, "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.split()


def test_ensure_window_creates_session_then_window(tmux_socket):
    tmux.ensure_window("claude", "tui", ["sleep", "60"], socket=tmux_socket)
    assert windows(tmux_socket, "claude") == ["tui"]
    # Idempotent while the window exists.
    assert tmux.ensure_window("claude", "tui", ["sleep", "60"], socket=tmux_socket) == []
    subprocess.run(
        ["tmux", "-L", tmux_socket, "new-window", "-t", "claude:", "-n", "work", "sleep 60"],
        check=True,
    )
    subprocess.run(["tmux", "-L", tmux_socket, "kill-window", "-t", "claude:tui"], check=True)
    ran = tmux.ensure_window("claude", "tui", ["sleep", "60"], socket=tmux_socket)
    assert ran[0][3] == "new-window"
    assert sorted(windows(tmux_socket, "claude")) == ["tui", "work"]
