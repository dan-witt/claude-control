import asyncio
import subprocess
import time

import pytest

from claude_control import launch, tmux
from claude_control.events import Event
from claude_control.launch import Launcher, LaunchError, ansi_c, command_line, find_process
from claude_control.state import LAUNCH_GRACE_S, Agent, Tracker

T0 = 1_000_000.0
TRICKY = 'it\'s "ok"\nsecond $HOME `x` \\ end\ttab \x01'


def test_ansi_c_round_trips_through_bash():
    out = subprocess.run(
        ["bash", "-c", f"printf %s {ansi_c(TRICKY)}"], capture_output=True, check=True
    )
    assert out.stdout.decode() == TRICKY
    assert "\n" not in ansi_c(TRICKY)


def test_command_line():
    line = command_line("claude", "sid-1", "my name", "hi there", ["--model", "opus"])
    assert line == "claude --session-id sid-1 --name 'my name' --model opus $'hi there'"
    assert command_line("claude", "sid-1", "n", None, ()) == "claude --session-id sid-1 --name n"


def test_default_name_avoids_taken_names():
    name = launch.default_name("/w/proj", set())
    assert name.startswith("proj-") and len(name) == len("proj-") + 2
    taken = {f"proj-{i:02x}" for i in range(256)}
    fallback = launch.default_name("/w/proj", taken)
    assert fallback not in taken and len(fallback) == len("proj-") + 6


def test_find_process(tmp_path):
    for pid, argv in {"10": [b"bash", b"-li"], "11": [b"claude", b"--session-id", b"abc"]}.items():
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    (tmp_path / "self").mkdir()
    assert find_process("abc", tmp_path) == 11
    assert find_process("zzz", tmp_path) is None


class FakeTmux:
    def __init__(self, fail_window=False):
        self.windows: list[tuple[str, str, str]] = []
        self.typed: list[tuple[str, str]] = []
        self.fail_window = fail_window

    async def new_window(self, name, cwd, command):
        if self.fail_window:
            raise tmux.TmuxError("no server")
        self.windows.append((name, cwd, command))
        return "%42"

    async def type_line(self, pane, text):
        self.typed.append((pane, text))


def launcher(tracker, fake, **kw):
    return Launcher(
        tracker, new_window=fake.new_window, type_line=fake.type_line, extra_args=("--x",), **kw
    )


def run(coro):
    return asyncio.run(coro)


def test_launch_registers_session_then_types_command(tmp_path):
    t, fake = Tracker(), FakeTmux()
    out = run(launcher(t, fake)(str(tmp_path), "do it", "job", T0))
    sid = out["session_id"]
    assert out == {"session_id": sid, "name": "job", "pane": "%42", "state": "starting"}
    assert fake.windows == [("job", str(tmp_path), "bash -li")]
    assert fake.typed == [("%42", f"claude --session-id {sid} --name job --x $'do it'")]
    s = t.sessions[sid]
    assert (s.state, s.name, s.cwd, s.awaiting_start) == ("starting", "job", str(tmp_path), True)
    assert [p.pane for p in s.processes] == ["%42"]


def test_launch_validates_cwd_and_name(tmp_path):
    t, fake = Tracker(), FakeTmux()
    with pytest.raises(LaunchError, match="absolute"):
        run(launcher(t, fake)("relative/dir", None, None, T0))
    with pytest.raises(LaunchError, match="no such directory"):
        run(launcher(t, fake)(str(tmp_path / "missing"), None, None, T0))
    run(launcher(t, fake)(str(tmp_path), None, "dup", T0))
    with pytest.raises(LaunchError, match="already running"):
        run(launcher(t, fake)(str(tmp_path), None, "dup", T0))
    with pytest.raises(LaunchError, match="no server"):
        run(launcher(t, FakeTmux(fail_window=True))(str(tmp_path), None, None, T0))
    assert len(fake.windows) == 1


def test_launch_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "proj").mkdir()
    t, fake = Tracker(), FakeTmux()
    out = run(launcher(t, fake)("~/proj", None, None, T0))
    assert fake.windows[0][1] == str(tmp_path / "proj")
    assert out["name"].startswith("proj-")


def launched(t: Tracker) -> str:
    s = t.launching("sid-l", "job", "/w", "%42", T0)
    return s.session_id


def session_start(t: Tracker, at: float):
    headers = {"x-tmux-pane": "%42", "x-claude-pid": "777", "x-claude-socket": "/run/s/777.sock"}
    payload = {"session_id": "sid-l", "hook_event_name": "SessionStart", "source": "startup"}
    e = Event.parse(at, payload, headers)
    assert e is not None
    t.apply(e)


def test_session_start_of_launched_session():
    t = Tracker()
    launched(t)
    session_start(t, T0 + 3)
    s = t.sessions["sid-l"]
    assert (s.state, s.awaiting_start) == ("idle", False)
    [p] = s.processes
    assert (p.pane, p.pid, p.socket) == ("%42", 777, "/run/s/777.sock")


def test_pending_launch_at_startup_prompt_waits_for_input():
    t = Tracker()
    launched(t)
    assert t.pending_launches(T0 + LAUNCH_GRACE_S - 1) == []
    assert t.pending_launches(T0 + LAUNCH_GRACE_S) == ["sid-l"]
    # Not listed by `claude agents` while at the folder trust prompt; not ended for it.
    t.reconcile([], {}, now=T0 + 60)
    assert t.sessions["sid-l"].state == "starting"
    t.check_launch("sid-l", running=True, now=T0 + 60)
    s = t.sessions["sid-l"]
    assert (s.state, s.reason, s.last_event) == ("waiting", "input", "waiting at startup")
    session_start(t, T0 + 90)  # user accepted the prompt
    assert (s.state, s.reason) == ("idle", None)


def test_pending_launch_whose_process_is_gone_failed():
    t = Tracker()
    launched(t)
    t.check_launch("sid-l", running=False, now=T0 + 20)
    s = t.sessions["sid-l"]
    assert (s.state, s.reason) == ("ended", "launch_failed")
    assert t.pending_launches(T0 + 30) == []


def test_reconcile_attaches_pid_once_launched_session_is_listed():
    t = Tracker()
    launched(t)
    session_start(t, T0 + 3)
    t.reconcile([Agent(777, "sid-l", "idle", name="job")], {777: "%42"}, now=T0 + 30)
    [p] = t.sessions["sid-l"].processes
    assert (p.pid, p.listed) == (777, True)


# --- tmux.new_window on an isolated server


@pytest.fixture
def tmux_socket():
    name = "cc-test-launch"
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, check=False)


def test_new_window_creates_session_then_windows(tmux_socket, tmp_path):
    first = run(tmux.new_window("claude", "one", str(tmp_path), "sleep 30", tmux_socket))
    second = run(tmux.new_window("claude", "two", str(tmp_path), "sleep 30", tmux_socket))
    assert first.startswith("%") and second.startswith("%") and first != second
    out = subprocess.run(
        [
            "tmux",
            "-L",
            tmux_socket,
            "list-panes",
            "-s",
            "-t",
            "claude",
            "-F",
            "#{window_name} #{pane_id} #{pane_current_path}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert f"one {first} {tmp_path}" in out and f"two {second} {tmp_path}" in out


def test_type_line_reaches_the_shell(tmux_socket, tmp_path):
    pane = run(tmux.new_window("claude", "sh", str(tmp_path), "bash --norc -i", tmux_socket))
    target = tmp_path / "out.txt"
    run(tmux.type_line(pane, f"printf %s {ansi_c(TRICKY)} > {target}", tmux_socket))
    deadline = time.monotonic() + 5
    while not target.exists() or target.read_text() != TRICKY:
        assert time.monotonic() < deadline, target.read_text() if target.exists() else "missing"
        time.sleep(0.05)
