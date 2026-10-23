import json

from claude_control import hooks_install

URL = "http://127.0.0.1:7777/hooks"

PEON = {"matcher": "", "hooks": [{"type": "command", "command": "peon.sh", "timeout": 10}]}


def settings_with_peon():
    return {"model": "opus", "hooks": {"Stop": [PEON], "SessionStart": [PEON]}}


def ours(settings, event):
    return [
        h for g in settings["hooks"].get(event, []) for h in g["hooks"] if hooks_install.is_ours(h)
    ]


def test_adds_every_event_and_keeps_existing_hooks():
    result = hooks_install.apply(settings_with_peon(), URL)
    assert result["model"] == "opus"
    for event in hooks_install.EVENTS:
        assert ours(result, event) == [hooks_install.hook_entry(event, URL)]
    assert result["hooks"]["Stop"][0] == PEON


def test_session_start_is_a_command_hook():
    # SessionStart does not support http hooks.
    entry = hooks_install.hook_entry("SessionStart", URL)
    assert entry["type"] == "command"
    assert URL in entry["command"]
    assert hooks_install.hook_entry("Stop", URL)["type"] == "http"


def test_session_start_command_posts_stdin(tmp_path):
    import subprocess

    # Replace curl with a stub that records its argv, then run the command through sh.
    stub = tmp_path / "curl"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OUT"\ncat > "$OUT.body"\n')
    stub.chmod(0o755)
    out = tmp_path / "argv"
    command = hooks_install.hook_entry("SessionStart", URL)["command"]
    subprocess.run(
        ["sh", "-c", command],
        input=b'{"x":1}',
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "OUT": str(out), "TMUX_PANE": "%7"},
        check=True,
    )
    argv = out.read_text().splitlines()
    assert "X-Tmux-Pane: %7" in argv
    assert any(a.startswith("X-Claude-Pid: ") and a[14:].isdigit() for a in argv)
    assert argv[-1] == URL
    assert (tmp_path / "argv.body").read_text() == '{"x":1}'


def test_idempotent():
    once = hooks_install.apply(settings_with_peon(), URL)
    assert hooks_install.apply(once, URL) == once


def test_port_change_replaces_entry():
    once = hooks_install.apply(settings_with_peon(), URL)
    moved = hooks_install.apply(once, "http://127.0.0.1:9999/hooks")
    assert [h["url"] for h in ours(moved, "Stop")] == ["http://127.0.0.1:9999/hooks"]
    [start] = ours(moved, "SessionStart")
    assert "9999" in start["command"] and "7777" not in start["command"]


def test_uninstall_restores_original():
    original = settings_with_peon()
    installed = hooks_install.apply(original, URL)
    assert hooks_install.apply(installed, None) == original


def test_other_http_hooks_are_not_ours():
    assert not hooks_install.is_ours({"type": "http", "url": "http://127.0.0.1:7777/other"})
    assert not hooks_install.is_ours({"type": "http", "url": "https://example.com/hooks"})
    assert not hooks_install.is_ours({"type": "command", "command": "curl 127.0.0.1/hooks"})


def test_write_backs_up_and_reports_no_change(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings_with_peon()))
    changed, backup = hooks_install.write(path, URL)
    assert changed and backup is not None
    assert json.loads(backup.read_text()) == settings_with_peon()
    assert ours(json.loads(path.read_text()), "PreToolUse")
    assert hooks_install.write(path, URL) == (False, None)


def test_uninstall_missing_file_is_noop(tmp_path):
    path = tmp_path / "settings.json"
    assert hooks_install.write(path, None) == (False, None)
    assert not path.exists()
