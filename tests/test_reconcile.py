import asyncio
import os
import stat

from claude_control import reconcile


def test_panes_for_walks_parent_pids():
    parents = {300: 200, 200: 100, 100: 1, 400: 1}
    pane_by_pid = {200: "%1", 500: "%2"}
    result = reconcile.panes_for([300, 200, 400, 500], pane_by_pid, parents.get)
    # 300 runs in the shell of pane %1; 200 is that shell; 400 is outside tmux.
    assert result == {300: "%1", 200: "%1", 500: "%2"}


def test_parent_pid_of_this_process():
    assert reconcile.parent_pid(os.getpid()) == os.getppid()
    assert reconcile.parent_pid(2**22 + 12345) is None


def fake_claude(tmp_path, script):
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_list_agents_parses_rows(tmp_path):
    rows = (
        '[{"pid": 5, "sessionId": "s1", "status": "busy", "cwd": "/w", "kind": "interactive",'
        ' "name": "w-1"}, {"pid": "bad"}]'
    )
    claude = fake_claude(tmp_path, f"echo '{rows}'\n")
    agents = asyncio.run(reconcile.list_agents(claude))
    assert agents is not None
    [a] = agents
    assert (a.pid, a.session_id, a.status, a.name) == (5, "s1", "busy", "w-1")


def test_list_agents_failure_is_none(tmp_path):
    assert asyncio.run(reconcile.list_agents(fake_claude(tmp_path, "exit 3\n"))) is None
    assert asyncio.run(reconcile.list_agents(fake_claude(tmp_path, "echo nope\n"))) is None
    assert asyncio.run(reconcile.list_agents(str(tmp_path / "missing"))) is None
