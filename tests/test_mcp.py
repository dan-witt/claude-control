import json
import socket
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.subscriptions import InMemorySubscriptionBus, ResourceUpdated
from mcp.types import TextContent, TextResourceContents

from claude_control import mcp_server, tmux
from claude_control.app import create_app
from claude_control.events import Event
from claude_control.launch import LaunchError
from claude_control.state import Agent, Tracker
from claude_control.store import EventStore

T0 = 1_000_000.0


def hook(tracker: Tracker, t: float, name: str, sid: str, headers=None, **payload):
    e = Event.parse(T0 + t, {"session_id": sid, "hook_event_name": name, **payload}, headers or {})
    assert e is not None
    tracker.apply(e)


@pytest.fixture
def tracker():
    t = Tracker()
    hook(
        t,
        0,
        "SessionStart",
        "aaaa-1111",
        {"x-tmux-pane": "%1", "x-claude-pid": "11"},
        source="startup",
        cwd="/w/a",
    )
    hook(t, 1, "UserPromptSubmit", "aaaa-1111", prompt="go")
    hook(t, 2, "PreToolUse", "aaaa-1111", tool_name="Bash", tool_use_id="t1")
    hook(t, 3, "PermissionRequest", "aaaa-1111", tool_name="Bash")
    hook(t, 0, "SessionStart", "bbbb-2222", {"x-claude-pid": "22"}, source="startup", cwd="/w/b")
    hook(t, 1, "SessionStart", "cccc-3333", source="startup")
    hook(t, 2, "SessionEnd", "cccc-3333", reason="other")
    t.reconcile(
        [Agent(11, "aaaa-1111", "waiting", name="a-1"), Agent(22, "bbbb-2222", "idle")],
        {11: "%1"},
        now=T0 + 5,
    )
    return t


class FakeFocus:
    def __init__(self, error: str | None = None):
        self.calls: list[str] = []
        self.error = error

    async def __call__(self, pane: str) -> str:
        self.calls.append(pane)
        if self.error:
            raise tmux.TmuxError(self.error)
        return "/dev/pts/9"


def server(tracker, focus=None):
    return mcp_server.build(tracker, InMemorySubscriptionBus(), focus or FakeFocus())


async def call(srv, tool, **args):
    async with Client(srv) as c:
        return await c.call_tool(tool, args)


def text(result) -> str:
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text


def structured(result):
    assert not result.is_error, result.content
    data = result.structured_content
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


@pytest.mark.anyio
async def test_tools_are_listed(tracker):
    async with Client(server(tracker)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"list_sessions", "get_session", "focus_session", "launch_session"}


@pytest.mark.anyio
async def test_list_sessions_defaults_to_live_sessions(tracker):
    rows = structured(await call(server(tracker), "list_sessions"))
    assert [(r["session_id"], r["state"], r["reason"]) for r in rows] == [
        ("aaaa-1111", "waiting", "permission"),
        ("bbbb-2222", "idle", None),
    ]
    a = rows[0]
    assert (a["name"], a["pane"], a["cwd"], a["current_tool"]) == ("a-1", "%1", "/w/a", "Bash")


@pytest.mark.anyio
async def test_list_sessions_board_fields(tracker):
    rows = structured(await call(server(tracker), "list_sessions"))
    a = rows[0]
    assert a["state_since"] == "1970-01-12T13:46:43.000+00:00"  # PermissionRequest at T0 + 3
    assert a["last_event"] == "PermissionRequest Bash"
    assert a["last_event_text"] == "asking to use Bash"
    assert a["last_event_at"] == "1970-01-12T13:46:43.000+00:00"
    assert a["replaces"] is None


def test_describe_events():
    describe = mcp_server.describe
    assert describe("PreToolUse Bash") == "running Bash"
    assert describe("PostToolUseFailure Read") == "ran Read (failed)"
    assert describe("PermissionRequest AskUserQuestion") == "asking a question"
    assert describe("PermissionRequest Bash") == "asking to use Bash"
    assert describe("Stop") == "finished its turn"
    assert describe("SessionEnd other") == "ended (other)"
    assert describe("task notification") == "background task reported"
    assert describe("Something New") == "Something New"


@pytest.mark.anyio
async def test_list_sessions_filters_by_state(tracker):
    rows = structured(await call(server(tracker), "list_sessions", states=["waiting"]))
    assert [r["session_id"] for r in rows] == ["aaaa-1111"]
    rows = structured(await call(server(tracker), "list_sessions", states=["ended"]))
    assert [r["session_id"] for r in rows] == ["cccc-3333"]
    result = await call(server(tracker), "list_sessions", states=["asleep"])
    assert result.is_error


@pytest.mark.anyio
@pytest.mark.parametrize("ref", ["aaaa-1111", "aaaa", "a-1"])
async def test_get_session_by_id_prefix_or_name(tracker, ref):
    s = structured(await call(server(tracker), "get_session", session_id=ref))
    assert s["session_id"] == "aaaa-1111"
    assert s["permission_tool"] == "Bash"
    assert s["processes"] == [{"pid": 11, "pane": "%1"}]


@pytest.mark.anyio
async def test_get_session_unknown_is_an_error(tracker):
    assert (await call(server(tracker), "get_session", session_id="zzzz")).is_error
    assert (await call(server(tracker), "get_session", session_id="a")).is_error  # too short


@pytest.mark.anyio
async def test_focus_session_switches_to_its_pane(tracker):
    focus = FakeFocus()
    out = structured(await call(server(tracker, focus), "focus_session", session_id="a-1"))
    assert out == {"session_id": "aaaa-1111", "pane": "%1", "client": "/dev/pts/9"}
    assert focus.calls == ["%1"]


@pytest.mark.anyio
async def test_focus_session_errors(tracker):
    focus = FakeFocus()
    outside = await call(server(tracker, focus), "focus_session", session_id="bbbb")
    assert outside.is_error and "not running in tmux" in text(outside)
    ended = await call(server(tracker, focus), "focus_session", session_id="cccc")
    assert ended.is_error and "ended" in text(ended)
    assert focus.calls == []
    detached = await call(
        server(tracker, FakeFocus("no tmux client is attached")), "focus_session", session_id="aaaa"
    )
    assert detached.is_error and "no tmux client" in text(detached)


@pytest.mark.anyio
async def test_sessions_resource(tracker):
    async with Client(server(tracker)) as c:
        uris = [str(r.uri) for r in (await c.list_resources()).resources]
        content = await c.read_resource(mcp_server.SESSIONS_URI)
    assert uris == [mcp_server.SESSIONS_URI]
    [body] = content.contents
    assert isinstance(body, TextResourceContents)
    rows = json.loads(body.text)
    assert [r["session_id"] for r in rows] == ["aaaa-1111", "bbbb-2222"]


@pytest.mark.anyio
async def test_resource_subscribers_get_updates(tracker):
    bus = InMemorySubscriptionBus()
    srv = mcp_server.build(tracker, bus, FakeFocus())
    async with (
        Client(srv) as c,
        c.listen(resource_subscriptions=[mcp_server.SESSIONS_URI]) as sub,
    ):
        await bus.publish(ResourceUpdated(uri=mcp_server.SESSIONS_URI))
        with anyio.fail_after(5):
            event = await sub.__anext__()
    assert event == ResourceUpdated(uri=mcp_server.SESSIONS_URI)


def test_signature_ignores_tool_activity(tracker):
    before = tracker.signature()
    hook(tracker, 10, "PostToolUse", "bbbb-2222", tool_name="Read", tool_use_id="t9")
    hook(tracker, 11, "PreToolUse", "bbbb-2222", tool_name="Read", tool_use_id="t10")
    assert tracker.signature() != before  # bbbb went from idle to running
    before = tracker.signature()
    hook(tracker, 12, "PostToolUse", "bbbb-2222", tool_name="Read", tool_use_id="t10")
    hook(tracker, 13, "PreToolUse", "bbbb-2222", tool_name="Grep", tool_use_id="t11")
    assert tracker.signature() == before


# --- over HTTP, through the daemon app


@pytest.fixture
def daemon(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    store = EventStore(tmp_path / "events.db")
    app = create_app(store, token="sekret", focus=FakeFocus())
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)
    store.close()


def test_mcp_requires_token(daemon):
    r = httpx2.post(f"{daemon}/mcp", json={}, headers={"accept": "application/json"})
    assert r.status_code == 401
    r = httpx2.post(f"{daemon}/mcp", json={}, headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_mcp_over_http_sees_hook_events(daemon):
    payload = {"session_id": "http-1", "hook_event_name": "UserPromptSubmit", "prompt": "x"}
    r = httpx2.post(f"{daemon}/hooks", json=payload)
    assert r.status_code == 204

    async with httpx2.AsyncClient(headers={"authorization": "Bearer sekret"}) as http:
        transport = streamable_http_client(f"{daemon}/mcp", http_client=http)
        async with Client(transport) as c:  # type: ignore[arg-type]
            result = await c.call_tool("list_sessions", {})
    rows = structured(result)
    assert [(r["session_id"], r["state"]) for r in rows] == [("http-1", "running")]


@pytest.mark.anyio
async def test_subscriber_is_notified_when_a_hook_changes_state(daemon):
    async with httpx2.AsyncClient(headers={"authorization": "Bearer sekret"}) as http:
        transport = streamable_http_client(f"{daemon}/mcp", http_client=http)
        async with (
            Client(transport) as c,  # type: ignore[arg-type]
            c.listen(resource_subscriptions=[mcp_server.SESSIONS_URI]) as sub,
            httpx2.AsyncClient() as plain,
        ):
            payload = {"session_id": "http-2", "hook_event_name": "SessionStart"}
            r = await plain.post(f"{daemon}/hooks", json=payload)
            assert r.status_code == 204
            with anyio.fail_after(5):
                event = await sub.__anext__()
    assert event == ResourceUpdated(uri=mcp_server.SESSIONS_URI)


@pytest.mark.anyio
async def test_launch_session_tool(tracker):
    calls = []

    async def launch(cwd, prompt, name):
        calls.append((cwd, prompt, name))
        if cwd == "/bad":
            raise LaunchError("no such directory: /bad")
        return {"session_id": "new-1", "name": "x-1", "pane": "%5", "state": "starting"}

    srv = mcp_server.build(tracker, InMemorySubscriptionBus(), FakeFocus(), launch)
    ok = structured(await call(srv, "launch_session", cwd="/w", prompt="hi"))
    assert ok["session_id"] == "new-1"
    bad = await call(srv, "launch_session", cwd="/bad")
    assert bad.is_error and "no such directory" in text(bad)
    assert calls == [("/w", "hi", None), ("/bad", None, None)]
