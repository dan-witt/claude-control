"""Extract a scrubbed event sequence from an events.db into a test fixture.

    uv run python scripts/fixture.py DB OUT.jsonl --session ID [--session ID] [--from N] [--to N]

Keeps event structure and ids; prompts, messages, tool input/output and paths are replaced,
so fixtures can come from sessions doing real work.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from claude_control.events import task_notices

KEEP = {
    "hook_event_name",
    "session_id",
    "agent_id",
    "agent_type",
    "tool_name",
    "tool_use_id",
    "source",
    "reason",
    "notification_type",
    "permission_mode",
    "prompt_id",
}
TOOL_INPUT_KEEP = {"subagent_type", "run_in_background"}
TOOL_RESPONSE_KEEP = {"status", "isAsync", "agentId", "backgroundTaskId", "taskId", "task_id"}
HEADERS = {"x-tmux-pane": "pane", "x-claude-socket": "socket", "x-claude-pid": "pid"}


def scrub_prompt(prompt: str) -> str:
    notices = task_notices(prompt)
    if not notices:
        return "<prompt>"
    parts = []
    for n in notices:
        body = f"<task-id>{n.task_id}</task-id>"
        if n.tool_use_id:
            body += f"<tool-use-id>{n.tool_use_id}</tool-use-id>"
        if n.status:
            body += f"<status>{n.status}</status>"
        parts.append(f"<task-notification>{body}</task-notification>")
    return "\n".join(parts)


def scrub(payload: dict) -> dict:
    out = {k: v for k, v in payload.items() if k in KEEP}
    if "error" in payload:
        # StopFailure carries an error code; PostToolUseFailure carries the tool's output.
        stop = payload.get("hook_event_name") == "StopFailure"
        out["error"] = payload["error"] if stop else "<error>"
    if "cwd" in payload:
        out["cwd"] = "/work/project"
    if isinstance(payload.get("prompt"), str):
        out["prompt"] = scrub_prompt(payload["prompt"])
    for key in ("last_assistant_message", "message"):
        if key in payload:
            out[key] = f"<{key}>"
    if isinstance(payload.get("tool_input"), dict):
        out["tool_input"] = {k: v for k, v in payload["tool_input"].items() if k in TOOL_INPUT_KEEP}
    if isinstance(payload.get("tool_response"), dict):
        out["tool_response"] = {
            k: v for k, v in payload["tool_response"].items() if k in TOOL_RESPONSE_KEEP
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--session", action="append", required=True)
    ap.add_argument("--from", dest="first", type=int, default=0)
    ap.add_argument("--to", dest="last", type=int, default=2**62)
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    marks = ",".join("?" for _ in args.session)
    rows = db.execute(
        f"SELECT received_at, headers, payload FROM events WHERE session_id IN ({marks})"
        " AND id BETWEEN ? AND ? ORDER BY id",
        (*args.session, args.first, args.last),
    ).fetchall()
    if not rows:
        raise SystemExit("no events")
    start = rows[0][0]
    with args.out.open("w") as f:
        for received_at, headers, payload in rows:
            h = json.loads(headers)
            record = {
                "t": round(received_at - start, 1),
                "headers": {short: h[long] for long, short in HEADERS.items() if h.get(long)},
                "payload": scrub(json.loads(payload)),
            }
            f.write(json.dumps(record) + "\n")
    print(f"{len(rows)} events -> {args.out}")


if __name__ == "__main__":
    main()
