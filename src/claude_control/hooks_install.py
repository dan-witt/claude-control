import json
import os
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "Notification",
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "StopFailure",
    "SessionEnd",
]

TIMEOUT_S = 1


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def hook_entry(event: str, url: str) -> dict:
    if event == "SessionStart":
        # SessionStart accepts only command and mcp_tool hooks. $PPID is the claude process.
        command = (
            f"curl -s -m {TIMEOUT_S} -o /dev/null -H 'Content-Type: application/json'"
            ' -H "X-Tmux-Pane: $TMUX_PANE" -H "X-Claude-Pid: $PPID"'
            ' -H "X-Claude-Socket: $CLAUDE_CODE_MESSAGING_SOCKET"'
            f" --data-binary @- {shlex.quote(url)} || true"
        )
        return {"type": "command", "command": command, "timeout": TIMEOUT_S + 1}
    # The messaging socket path is unique per claude process; it tells apart two
    # processes resumed on the same session_id outside tmux.
    return {
        "type": "http",
        "url": url,
        "timeout": TIMEOUT_S,
        "headers": {
            "X-Tmux-Pane": "$TMUX_PANE",
            "X-Claude-Socket": "$CLAUDE_CODE_MESSAGING_SOCKET",
        },
        "allowedEnvVars": ["TMUX_PANE", "CLAUDE_CODE_MESSAGING_SOCKET"],
    }


def _is_our_url(value: str) -> bool:
    parts = urlsplit(value)
    return (
        parts.scheme == "http"
        and parts.hostname in ("127.0.0.1", "localhost")
        and parts.path == "/hooks"
    )


def is_ours(hook: dict) -> bool:
    """A hook delivering to a claude-control /hooks endpoint on this machine, any port."""
    if hook.get("type") == "http":
        return _is_our_url(hook.get("url", ""))
    if hook.get("type") == "command":
        try:
            words = shlex.split(hook.get("command", ""))
        except ValueError:
            return False
        return bool(words) and words[0] == "curl" and any(_is_our_url(w) for w in words)
    return False


def _strip_ours(groups: list) -> list:
    kept = []
    for group in groups:
        hooks = [h for h in group.get("hooks", []) if not is_ours(h)]
        if hooks:
            kept.append({**group, "hooks": hooks})
    return kept


def apply(settings: dict, url: str | None) -> dict:
    """Return settings with our hooks replaced by ones for `url`, or removed when url is None."""
    hooks = dict(settings.get("hooks", {}))
    for event in EVENTS:
        groups = _strip_ours(hooks.get(event, []))
        if url is not None:
            groups.append({"matcher": "", "hooks": [hook_entry(event, url)]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    result = dict(settings)
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


def write(path: Path, url: str | None) -> tuple[bool, Path | None]:
    """Rewrite the settings file with our hooks set to `url` (None removes them).

    Returns (changed, backup path). No backup is made for a file that did not exist.
    """
    settings = json.loads(path.read_text()) if path.exists() else {}
    updated = apply(settings, url)
    if updated == settings:
        return False, None
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak.claude-control.{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(updated, f, indent=2)
        f.write("\n")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return True, backup
