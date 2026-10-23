import json

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
