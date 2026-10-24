import argparse
import json
import logging
import subprocess
import time
import urllib.request
from pathlib import Path

from . import config, hooks_install, service_install
from .store import EventStore


def cmd_serve(args: argparse.Namespace, cfg: config.Config) -> None:
    import uvicorn

    from .app import create_app, system_listing

    store = EventStore(cfg.db_path)
    app = create_app(
        store,
        token=config.ensure_token(),
        retention_days=cfg.retention_days,
        replay_hours=cfg.replay_hours,
        listing=system_listing(cfg.claude_bin),
        reconcile_interval_s=cfg.reconcile_interval_s,
    )
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=args.log_level)


def cmd_install_hooks(args: argparse.Namespace, cfg: config.Config) -> None:
    path = args.settings or hooks_install.default_settings_path()
    url = None if args.uninstall else cfg.hooks_url
    changed, backup = hooks_install.write(path, url)
    if not changed:
        print(f"{path} unchanged")
        return
    action = "removed from" if url is None else f"pointing at {url} written to"
    print(f"hooks {action} {path}" + (f" (backup: {backup})" if backup else ""))


MCP_NAME = "claude-control"


def cmd_install_mcp(args: argparse.Namespace, cfg: config.Config) -> None:
    """Register the daemon's MCP endpoint with Claude Code at user scope."""
    claude = cfg.claude_bin
    remove = [claude, "mcp", "remove", "--scope", "user", MCP_NAME]
    subprocess.run(remove, check=False, capture_output=True)  # absent is fine
    if args.uninstall:
        print(f"removed MCP server {MCP_NAME}")
        return
    url = f"http://{cfg.host}:{cfg.port}/mcp"
    header = f"Authorization: Bearer {config.ensure_token()}"
    add = [claude, "mcp", "add", "--scope", "user", "--transport", "http", MCP_NAME, url]
    subprocess.run([*add, "--header", header], check=True, capture_output=True)
    print(f"registered MCP server {MCP_NAME} at {url} (user scope)")


def cmd_install_service(args: argparse.Namespace, cfg: config.Config) -> None:
    exe = service_install.executable()
    path = service_install.install(exe, start=not args.no_start)
    print(f"wrote {path} (ExecStart={exe} serve)")


def cmd_events(args: argparse.Namespace, cfg: config.Config) -> None:
    store = EventStore(cfg.db_path)
    for e in store.recent(limit=args.limit, session_id=args.session):
        if args.raw:
            print(e.payload)
            continue
        stamp = time.strftime("%H:%M:%S", time.localtime(e.received_at))
        payload = json.loads(e.payload)
        detail = (
            payload.get("notification_type")
            or payload.get("tool_name")
            or payload.get("source")
            or payload.get("reason")
            or ""
        )
        print(f"{stamp} {e.session_id} {e.tmux_pane or '-':>5} {e.event} {detail}")


def _age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def cmd_sessions(args: argparse.Namespace, cfg: config.Config) -> None:
    url = f"http://{cfg.host}:{cfg.port}/sessions" + ("?ended=1" if args.all else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            sessions = json.load(r)
    except OSError as e:
        raise SystemExit(f"daemon not reachable at {url}: {e}") from e
    if args.json:
        print(json.dumps(sessions, indent=2))
        return
    now = time.time()
    for s in sessions:
        state = s["state"] + (f"/{s['reason']}" if s["reason"] else "")
        panes = ",".join(p["pane"] for p in s["processes"] if p["pane"]) or "-"
        tasks = f" +{len(s['tasks'])} bg" if s["tasks"] else ""
        tool = f" [{s['current_tool']}]" if s["current_tool"] else ""
        label = (s["name"] or s["session_id"][:8])[:28]
        print(
            f"{state:<20} {label:<28} {panes:<6} {_age(now - s['last_event_at']):>5}"
            f" {s['last_event']}{tool}{tasks}  {s['cwd'] or ''}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="claude-control")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the daemon")
    p.add_argument("--log-level", default="info")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("install-hooks", help="add HTTP hooks to Claude Code settings")
    p.add_argument("--settings", type=Path)
    p.add_argument("--uninstall", action="store_true")
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("install-mcp", help="register the MCP endpoint with Claude Code")
    p.add_argument("--uninstall", action="store_true")
    p.set_defaults(func=cmd_install_mcp)

    p = sub.add_parser("install-service", help="install and start the systemd user unit")
    p.add_argument("--no-start", action="store_true")
    p.set_defaults(func=cmd_install_service)

    p = sub.add_parser("sessions", help="print the daemon's session state")
    p.add_argument("-a", "--all", action="store_true", help="include ended sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("events", help="print recently received hook events")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("-s", "--session")
    p.add_argument("--raw", action="store_true", help="print raw JSON payloads")
    p.set_defaults(func=cmd_events)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    args.func(args, config.load())
