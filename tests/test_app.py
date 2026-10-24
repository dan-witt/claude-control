import json
import time

import pytest
from starlette.testclient import TestClient

from claude_control.app import create_app
from claude_control.store import EventStore


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "events.db")
    yield s
    s.close()


@pytest.fixture
def client(store):
    with TestClient(create_app(store), base_url="http://127.0.0.1:7777") as c:
        yield c


def post(client, payload, **headers):
    return client.post(
        "/hooks",
        content=json.dumps(payload),
        headers={"content-type": "application/json", **headers},
    )


def test_stores_raw_payload_with_pane(client, store):
    payload = {"session_id": "s1", "hook_event_name": "Stop", "last_assistant_message": "done"}
    r = post(client, payload, **{"x-tmux-pane": "%3"})
    assert r.status_code == 204
    assert r.content == b""
    assert r.headers["connection"] == "close"
    [e] = store.recent()
    assert (e.session_id, e.event, e.tmux_pane) == ("s1", "Stop", "%3")
    assert json.loads(e.payload) == payload
    assert e.headers["x-tmux-pane"] == "%3"


def test_empty_pane_header_stored_as_null(client, store):
    post(client, {"session_id": "s1", "hook_event_name": "Stop"}, **{"x-tmux-pane": ""})
    assert store.recent()[0].tmux_pane is None


def test_rejects_non_json_content_type(client, store):
    r = client.post("/hooks", content="{}", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    assert r.headers["connection"] == "close"
    assert store.recent() == []


def test_rejects_invalid_json(client, store):
    r = client.post("/hooks", content="{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert store.recent() == []


def test_rejects_non_local_host(client, store):
    r = post(client, {"session_id": "s1"}, host="evil.example")
    assert r.status_code == 400
    assert store.recent() == []


def test_accepts_localhost_name(client, store):
    r = post(client, {"session_id": "s1"}, host="localhost:7777")
    assert r.status_code == 204


def test_prune(store):
    store.append("{}", "old", "Stop", None, {}, received_at=100.0)
    store.append("{}", "new", "Stop", None, {}, received_at=300.0)
    assert store.prune(200.0) == 1
    assert [e.session_id for e in store.recent()] == ["new"]


def test_warns_when_handling_reaches_hook_timeout(store, caplog):
    with TestClient(create_app(store, slow_after_s=0), base_url="http://127.0.0.1:7777") as c:
        post(c, {"session_id": "s1", "hook_event_name": "Stop"})
    [record] = [r for r in caplog.records if r.name == "claude_control.app"]
    assert record.levelname == "WARNING"
    assert "Stop" in record.getMessage() and "s1" in record.getMessage()


def test_no_warning_for_fast_request(client, caplog):
    post(client, {"session_id": "s1", "hook_event_name": "Stop"})
    assert not [r for r in caplog.records if r.name == "claude_control.app"]


def test_hook_updates_session_state(client):
    sid = "s-state"
    post(
        client,
        {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
        **{"x-tmux-pane": "%4", "x-claude-pid": "42"},
    )
    post(client, {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    [s] = client.get("/sessions").json()
    assert (s["session_id"], s["state"], s["last_event"]) == (sid, "running", "prompt")
    assert s["processes"] == [{"pid": 42, "pane": "%4", "socket": None}]


def test_sessions_hides_ended_unless_asked(client):
    post(client, {"session_id": "gone", "hook_event_name": "SessionStart", "source": "startup"})
    post(client, {"session_id": "gone", "hook_event_name": "SessionEnd", "reason": "other"})
    assert client.get("/sessions").json() == []
    [s] = client.get("/sessions?ended=1").json()
    assert (s["state"], s["reason"]) == ("ended", "other")


def test_startup_replays_stored_events(store):
    payload = {"session_id": "old", "hook_event_name": "Stop"}
    store.append(json.dumps(payload), "old", "Stop", None, {})
    with TestClient(create_app(store), base_url="http://127.0.0.1:7777") as c:
        [s] = c.get("/sessions").json()
    assert (s["session_id"], s["state"], s["reason"]) == ("old", "waiting", "done")


def test_reconcile_loop_applies_listing(store):
    from claude_control.state import Agent

    async def listing():
        return [Agent(77, "listed", "busy", name="w-2")], {77: "%9"}

    app = create_app(store, listing=listing, reconcile_interval_s=0.05)
    rows = []
    with TestClient(app, base_url="http://127.0.0.1:7777") as c:
        for _ in range(100):
            rows = c.get("/sessions").json()
            if rows:
                break
            time.sleep(0.02)
    [s] = rows
    assert (s["state"], s["name"], s["processes"][0]["pane"]) == ("running", "w-2", "%9")
