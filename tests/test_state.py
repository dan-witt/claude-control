"""State machine against recorded hook sequences (tests/fixtures, made with scripts/fixture.py)."""

import json
from pathlib import Path

import pytest

from claude_control.events import Event
from claude_control.state import Agent, Tracker

FIXTURES = Path(__file__).parent / "fixtures"
T0 = 1_000_000.0
HEADERS = {"pane": "x-tmux-pane", "socket": "x-claude-socket", "pid": "x-claude-pid"}


def load(name: str) -> list[Event]:
    events = []
    for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines():
        r = json.loads(line)
        headers = {HEADERS[k]: str(v) for k, v in r["headers"].items()}
        e = Event.parse(T0 + r["t"], r["payload"], headers)
        assert e is not None
        events.append(e)
    return events


class Replay:
    def __init__(self, name: str):
        self.events = load(name)
        self.tracker = Tracker()
        self.pos = 0

    def to(self, index: int) -> "Replay":
        """Apply events up to and including fixture line `index`."""
        while self.pos <= index:
            self.tracker.apply(self.events[self.pos])
            self.pos += 1
        return self

    def all(self) -> "Replay":
        return self.to(len(self.events) - 1)

    def s(self, prefix: str):
        [session] = [s for s in self.tracker.sessions.values() if s.session_id.startswith(prefix)]
        return session

    def at(self, index: int, prefix: str) -> tuple[str, str | None]:
        s = self.to(index).s(prefix)
        return s.state, s.reason


def test_walkthrough_turns_and_background_shell():
    r = Replay("walkthrough")
    s = r.to(0).s("e7cb")
    assert (s.state, s.reason) == ("idle", None)
    [p] = s.processes
    assert (p.pid, p.pane, p.socket) == (1393089, "%0", "/run/user/1000/cc-socks/1393089.sock")

    assert r.at(3, "e7cb") == ("running", None)
    r.to(5)
    assert r.s("e7cb").current_tool == "Bash"
    r.to(6)
    s = r.s("e7cb")
    assert s.current_tool is None and s.tool_count == 1
    assert {t.task_id: t.kind for t in s.tasks.values()} == {"b1upw7r0f": "shell"}

    assert r.at(8, "e7cb") == ("waiting", "done")
    assert r.s("e7cb").message == "<last_assistant_message>"
    r.to(9)  # SubagentStop from an internal agent does not create a task
    assert set(r.s("e7cb").tasks) == {"b1upw7r0f"}

    assert r.at(14, "e7cb") == ("running", None)  # task notification starts a turn
    assert r.s("e7cb").tasks == {}
    assert r.at(15, "e7cb") == ("waiting", "done")
    assert r.at(20, "e7cb") == ("waiting", "done")  # idle_prompt after Stop changes nothing


def test_walkthrough_tool_failure_clears_current_tool():
    r = Replay("walkthrough")
    r.to(40)
    assert r.s("e7cb").current_tool == "Read"
    count = r.s("e7cb").tool_count
    r.to(41)
    assert r.s("e7cb").current_tool is None
    assert r.s("e7cb").tool_count == count + 1


def test_walkthrough_permission_then_new_prompt():
    r = Replay("walkthrough")
    assert r.at(50, "2be0") == ("waiting", "permission")
    assert r.s("2be0").permission_tool == "Bash"
    assert r.at(51, "2be0") == ("running", None)  # user pressed Esc and typed a new prompt
    assert r.at(53, "2be0") == ("waiting", "permission")
    # idle_prompt is not sent while a prompt is open, so here the prompt was dismissed.
    assert r.at(55, "2be0") == ("waiting", "done")
    assert r.s("2be0").current_tool is None


def test_walkthrough_ask_user_question():
    r = Replay("walkthrough")
    assert r.at(58, "e7cb") == ("waiting", "input")
    assert r.at(59, "e7cb") == ("waiting", "input")  # permission_prompt keeps the input reason
    assert r.s("e7cb").message == "<message>"
    assert r.at(60, "e7cb") == ("running", None)
    assert r.at(62, "e7cb") == ("waiting", "done")


def test_walkthrough_compact_keeps_state_and_resume_moves_process():
    r = Replay("walkthrough")
    assert r.at(67, "e7cb") == ("waiting", "done")
    assert r.at(69, "e7cb") == ("ended", "resume")
    r.to(70)
    s = r.s("2be0")
    assert {p.pane for p in s.processes} == {"%0", "%2"}
    assert {p.pid for p in s.processes} == {1393089, 1413665}
    assert r.s("e7cb").processes == []

    # SessionEnd on exit has no socket header; it is matched by pane.
    r.to(71)
    assert [p.pane for p in r.s("2be0").processes] == ["%2"]
    assert r.s("2be0").state != "ended"
    assert r.at(72, "2be0") == ("ended", "other")


def test_idle_after_ten_minutes_of_done():
    r = Replay("walkthrough").to(18)  # Stop at t=104.0
    stop_at = T0 + 104.0
    r.tracker.tick(stop_at + 599)
    assert r.s("e7cb").state == "waiting"
    r.tracker.tick(stop_at + 601)
    assert (r.s("e7cb").state, r.s("e7cb").reason) == ("idle", None)
    assert r.at(21, "e7cb") == ("running", None)


def test_idle_waits_for_background_tasks():
    r = Replay("walkthrough").to(8)  # Stop with a background shell still open
    r.tracker.tick(T0 + 12.1 + 3600)
    assert r.s("e7cb").state == "waiting"


def test_clear_ends_old_session_and_moves_process():
    r = Replay("clear")
    assert r.at(3, "f137") == ("waiting", "permission")
    assert r.at(4, "f137") == ("waiting", "permission")
    assert r.at(5, "f137") == ("running", None)
    assert r.at(6, "f137") == ("waiting", "done")
    assert r.at(7, "f137") == ("ended", "clear")
    r.to(8)
    s = r.s("0dbf")
    assert (s.state, [p.pid for p in s.processes], [p.pane for p in s.processes]) == (
        "idle",
        [1309066],
        ["%0"],
    )
    assert r.at(9, "0dbf") == ("ended", "prompt_input_exit")


def test_plan_approval_is_a_permission_wait():
    r = Replay("plan_approval")
    assert r.at(10, "4122") == ("waiting", "permission")
    assert r.s("4122").permission_tool == "ExitPlanMode"
    assert r.at(12, "4122") == ("ended", "other")


def test_stop_failure_is_error_until_next_prompt():
    r = Replay("stop_failure")
    assert r.at(4, "1be9") == ("error", "unknown")
    assert r.s("1be9").message == "<last_assistant_message>"
    assert r.at(5, "1be9") == ("ended", "other")


def test_hook_driven_mcp_permission_clears_on_next_tool():
    r = Replay("hook_mcp_permission")
    assert r.at(5, "faa9") == ("waiting", "permission")
    assert r.at(8, "faa9") == ("running", None)
    assert r.at(11, "faa9") == ("waiting", "done")
    assert r.s("faa9").current_tool is None  # Stop clears calls that never posted


def test_parallel_tool_calls_overlap():
    r = Replay("parallel_bash")
    r.to(11)
    s = r.s("1eda")
    assert len(s.open_tools) == 4
    r.to(13)
    assert len(s.open_tools) == 2
    r.to(15)
    assert s.open_tools == {}
    assert s.tool_count == 7


def test_user_started_fork_is_background_activity():
    r = Replay("user_fork")
    first_fork_event = next(i for i, e in enumerate(r.events) if e.agent_id)
    r.to(first_fork_event - 1)
    before = (r.s("1eda").state, r.s("1eda").tool_count, r.s("1eda").last_event_at)
    r.to(first_fork_event)
    s = r.s("1eda")
    assert (s.state, s.tool_count, s.last_event_at) == before
    [task] = s.tasks.values()
    assert (task.kind, task.agent_type) == ("agent", "fork")
    assert task.task_id.startswith("ais-there-a")

    assert r.at(50, "1eda") == ("waiting", "done")
    assert len(r.s("1eda").tasks) == 1
    assert r.at(51, "1eda") == ("running", None)
    assert r.s("1eda").tasks == {}
    assert r.all().s("1eda").state == "waiting"


def test_model_launched_forks():
    r = Replay("model_forks")
    r.to(11)  # six Agent calls emitted, none launched yet
    s = r.s("1eda")
    assert (s.state, len(s.open_tools), s.current_tool) == ("running", 6, "Agent")
    r.to(17)
    assert s.open_tools == {}
    assert {t.kind for t in s.tasks.values()} == {"agent"}
    assert len(s.tasks) == 6

    # Fork tool events refresh their task and leave the session alone.
    r.to(22)
    count = s.tool_count
    r.to(109)
    assert s.tool_count == count
    assert len(s.tasks) == 6
    # The main agent's Stop after launching the forks was lost (socket hang up).
    assert s.state == "running"

    r.to(110)
    assert len(s.tasks) == 5
    assert r.at(126, "1eda") == ("waiting", "done")

    r.all()
    assert (s.state, s.reason) == ("waiting", "done")
    assert {t.task_id: t.kind for t in s.tasks.values()} == {"bgglb5o24": "shell"}


def test_event_for_unknown_session_creates_it():
    r = Replay("model_forks").to(0)  # fixture starts mid-session, no SessionStart
    s = r.s("1eda")
    assert s.state == "running"
    [p] = s.processes
    assert (p.socket, p.pid) == ("/run/user/1000/cc-socks/2126181.sock", None)


@pytest.mark.parametrize("name", [p.stem for p in FIXTURES.glob("*.jsonl")])
def test_every_fixture_replays(name):
    r = Replay(name).all()
    assert r.tracker.sessions


# --- reconciliation (`claude agents --json` rows)

E7CB_PID = 1393089
TWO_BE0_PID = 1413665


def agent(pid, session_prefix_or_id, status, tracker=None, **kw):
    sid = session_prefix_or_id
    if tracker is not None:
        [sid] = [k for k in tracker.sessions if k.startswith(session_prefix_or_id)]
    return Agent(pid=pid, session_id=sid, status=status, **kw)


def test_reconcile_discovers_sessions_started_before_the_daemon():
    t = Tracker()
    t.reconcile(
        [
            Agent(10, "s-busy", "busy", cwd="/w", kind="interactive", name="w-1"),
            Agent(11, "s-wait", "waiting"),
            Agent(12, "s-idle", "idle"),
        ],
        panes={10: "%3"},
        now=T0,
    )
    busy = t.sessions["s-busy"]
    assert (busy.state, busy.name, busy.cwd, busy.kind) == ("running", "w-1", "/w", "interactive")
    assert [(p.pid, p.pane, p.listed) for p in busy.processes] == [(10, "%3", True)]
    assert (t.sessions["s-wait"].state, t.sessions["s-wait"].reason) == ("waiting", "permission")
    assert t.sessions["s-idle"].state == "idle"
    assert t.sessions["s-idle"].processes[0].pane is None  # outside tmux


def test_reconcile_ends_session_whose_pid_is_gone():
    r = Replay("walkthrough").to(8)
    r.tracker.reconcile([], {}, now=T0 + 100)
    assert r.at(8, "e7cb") == ("ended", "process_gone")
    assert r.s("e7cb").tasks == {}


def test_reconcile_keeps_new_process_not_yet_listed():
    r = Replay("walkthrough").to(0)
    r.tracker.reconcile([], {}, now=T0 + 5)
    assert r.s("e7cb").state == "idle"


def test_reconcile_ends_listed_process_once_it_disappears():
    r = Replay("walkthrough").to(0)
    r.tracker.reconcile([agent(E7CB_PID, "e7cb", "idle", r.tracker)], {}, now=T0 + 1)
    r.tracker.reconcile([], {}, now=T0 + 2)
    assert r.s("e7cb").state == "ended"


def test_reconcile_idle_status_clears_dismissed_permission_prompt():
    r = Replay("walkthrough").to(53)  # PermissionRequest at t=230.4
    rows = lambda status: [
        agent(E7CB_PID, "e7cb", "idle", r.tracker),
        agent(TWO_BE0_PID, "2be0", status, r.tracker),
    ]
    r.tracker.reconcile(rows("idle"), {}, now=T0 + 235)  # too soon after the event
    assert r.s("2be0").state == "waiting"
    r.tracker.reconcile(rows("waiting"), {}, now=T0 + 260)  # prompt still open
    assert (r.s("2be0").state, r.s("2be0").reason) == ("waiting", "permission")
    r.tracker.reconcile(rows("idle"), {}, now=T0 + 260)
    assert (r.s("2be0").state, r.s("2be0").reason) == ("waiting", "done")
    assert r.s("2be0").current_tool is None


def test_reconcile_idle_status_ends_interrupted_turn():
    r = Replay("walkthrough").to(40)  # PreToolUse Read, never posted
    r.tracker.reconcile([agent(E7CB_PID, "e7cb", "idle", r.tracker)], {}, now=T0 + 230)
    s = r.s("e7cb")
    assert (s.state, s.reason, s.current_tool) == ("waiting", "done", None)


def test_reconcile_waiting_status_marks_missed_prompt():
    r = Replay("walkthrough").to(3)
    r.tracker.reconcile([agent(E7CB_PID, "e7cb", "waiting", r.tracker)], {}, now=T0 + 60)
    assert (r.s("e7cb").state, r.s("e7cb").reason) == ("waiting", "permission")


def test_reconcile_busy_status_does_not_override_hooks():
    r = Replay("walkthrough").to(8)  # Stop, background shell still running
    r.tracker.reconcile([agent(E7CB_PID, "e7cb", "busy", r.tracker)], {}, now=T0 + 60)
    s = r.s("e7cb")
    assert (s.state, s.reason, list(s.tasks)) == ("waiting", "done", ["b1upw7r0f"])


def test_reconcile_idle_status_closes_background_tasks():
    r = Replay("walkthrough").to(8)
    r.tracker.reconcile([agent(E7CB_PID, "e7cb", "idle", r.tracker)], {}, now=T0 + 60)
    assert r.s("e7cb").tasks == {}
    r.tracker.tick(T0 + 12.1 + 601)
    assert r.s("e7cb").state == "idle"


def test_reconcile_fills_in_pid_and_pane_for_process_seen_only_by_socket():
    r = Replay("model_forks").to(0)
    r.tracker.reconcile([agent(2126181, "1eda", "busy", r.tracker)], {2126181: "%7"}, now=T0 + 1)
    [p] = r.s("1eda").processes
    assert (p.pid, p.pane, p.socket) == (2126181, "%7", "/run/user/1000/cc-socks/2126181.sock")


def test_reconcile_moves_process_listed_under_another_session():
    r = Replay("walkthrough").to(0)
    r.tracker.reconcile([Agent(E7CB_PID, "new-session", "idle")], {}, now=T0 + 30)
    assert r.s("e7cb").state == "ended"
    assert [p.pid for p in r.tracker.sessions["new-session"].processes] == [E7CB_PID]


def test_reconcile_revives_ended_session_that_is_listed_again():
    r = Replay("walkthrough").to(0)
    r.tracker.reconcile([], {}, now=T0 + 100)
    assert r.s("e7cb").state == "ended"
    r.tracker.reconcile([agent(E7CB_PID, "e7cb", "idle", r.tracker)], {}, now=T0 + 120)
    assert (r.s("e7cb").state, r.s("e7cb").ended_at) == ("idle", None)


def test_first_hook_from_discovered_process_adopts_it():
    t = Tracker()
    t.reconcile([Agent(50, "found", "idle")], {50: "%2"}, now=T0)
    e = Event.parse(
        T0 + 20,
        {"session_id": "found", "hook_event_name": "UserPromptSubmit", "prompt": "x"},
        {"x-claude-socket": "/run/s/50.sock", "x-tmux-pane": "%2"},
    )
    assert e is not None
    t.apply(e)
    [p] = t.sessions["found"].processes
    assert (p.pid, p.socket, p.pane) == (50, "/run/s/50.sock", "%2")


def test_event_with_socket_adopts_process_from_events_without_one():
    t = Tracker()
    for headers in ({}, {"x-claude-socket": "/run/s/9.sock"}):
        e = Event.parse(T0, {"session_id": "old", "hook_event_name": "Stop"}, headers)
        assert e is not None
        t.apply(e)
    assert [p.socket for p in t.sessions["old"].processes] == ["/run/s/9.sock"]
