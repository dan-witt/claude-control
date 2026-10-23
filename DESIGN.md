# claude-control design

Ticket: [ECH-17](https://linear.app/echowire/issue/ECH-17). Decided 2026-10-23.

claude-control is a systemd user service that receives Claude Code hook events, tracks every running Claude Code session on the machine, and exposes those sessions over MCP. A Textual TUI is its first MCP client: it lists sessions, focuses a session's tmux pane, and launches new sessions in tmux.

It is the session-daemon component of the [Agent Status Board design](https://claude.ai/artifact/95onJgMf4EDciR5mD3wUjn). The board, voice agent, calendar and tasks are separate components that read from this one. Differences from that doc: calendar merging is not in this daemon, `launch_session` is in the vocabulary, and `whats_blocked` is `list_sessions(states=["waiting"])`.

## Scope

In: hook intake, session state, the MCP server, the TUI.
Out: calendar, tasks, the board UI, voice. No tool sends input to a session (no messaging socket, no `tmux send-keys` into Claude); a session is answered in its own terminal.

## Runtime

- Python + uv. One Starlette app serves `/hooks` and `/mcp` (streamable HTTP, official `mcp` SDK) on `127.0.0.1:7777` (configurable). TUI in Textual.
- Packaged as `claude-control`, installed with `uv tool install`. Subcommands: `serve`, `tui`, `install-hooks`, `install-service`.
- systemd **user** unit written by `install-service` to `~/.config/systemd/user/claude-control.service`, `Restart=on-failure`, no linger. A user unit, not a system unit, because launched Claude sessions need the desktop session's environment (`SSH_AUTH_SOCK`, `DBUS_SESSION_BUS_ADDRESS`, `DISPLAY`, `GPG_AGENT_INFO`), which the user manager carries and a system service does not.
- Config: `$XDG_CONFIG_HOME/claude-control/config.toml`. State: SQLite under `$XDG_STATE_HOME/claude-control/` with a sessions table and an events table; events older than 14 days are pruned.
- Tooling: pytest, ruff, pyright.

## Security

- Bind `127.0.0.1` only.
- `/mcp`: bearer token from `$XDG_CONFIG_HOME/claude-control/token` (mode 0600, generated on first run), plus `Host`/`Origin` must be localhost. `launch_session` runs Claude with an arbitrary prompt, so a web page reaching localhost must not be able to call it.
- `/hooks`: no token. Requires `Content-Type: application/json` (forces a CORS preflight) and a localhost `Host` (blocks DNS rebinding). This check may be dropped if it breaks hook delivery.

## Intake

Hooks are installed idempotently by `install-hooks` into `~/.claude/settings.json` (backup first; existing hooks such as peon-ping are left in place). All post the event JSON to `http://127.0.0.1:7777/hooks`:

- `UserPromptSubmit`, `Notification`, `PermissionRequest`, `PreToolUse`, `PostToolUse`, `Stop`, `StopFailure`, `SessionEnd`: HTTP hooks, `timeout: 2`, header `"X-Tmux-Pane": "$TMUX_PANE"` (`allowedEnvVars: ["TMUX_PANE"]`).
- `SessionStart`: a command hook running `curl`, because SessionStart accepts only `command` and `mcp_tool` hooks. It sends `X-Tmux-Pane` and `X-Claude-Pid: $PPID` (the claude process), and ends in `|| true` so a stopped daemon never fails the hook.

A failed HTTP hook is non-blocking in Claude Code, so a stopped daemon does not affect sessions. Running sessions pick up settings changes without a restart.

Reconciliation: at startup and every 15s the daemon runs `claude agents --json` (documented CLI; the underlying `~/.claude/sessions/*.json` registry is undocumented and not read directly). This discovers sessions started before the daemon and marks sessions `ended` when their pid disappears (kill -9, closed terminal). Background (`--bg`) sessions are listed, view-only.

## Session model

- Primary key: `session_id`. Each session holds a set of attached processes `{pid, tmux_pane}`; in-memory maps `pid → session_id` and `pane → session_id` resolve lookups.
- One `session_id` can have several processes: `claude --resume` of the same session in two terminals runs two pids with one id. `focus_session` picks the process with the most recent hook event.
- `/clear`: the old `session_id` is marked `ended` (reason `clear`) and its pane moves to the new `session_id`.

States (stable names; may be revised after milestone 1 discovery):

| State | Set by |
| --- | --- |
| `starting` | `launch_session`, until `SessionStart` arrives; `error` with reason `launch_timeout` after ~15s |
| `running` | `UserPromptSubmit`, or tool activity after a wait |
| `waiting` | reason `permission` (`PermissionRequest`, `Notification` `permission_prompt`), `input` (`Notification` `idle_prompt` / `agent_needs_input` / elicitation), `done` (`Stop`) |
| `idle` | `waiting/done` with no event for 10 min |
| `error` | `StopFailure` |
| `ended` | `SessionEnd`, or pid gone at reconciliation |

Activity: `PreToolUse` sets `current_tool` and increments a tool counter; `PostToolUse` clears `current_tool` and increments it. The TUI shows a spinner while `current_tool` is set or the counter changed in the last few seconds.

## tmux

- tmux is the session host. Sessions outside tmux are tracked and listed but cannot be focused.
- Launched sessions go into a dedicated tmux session `claude` (configurable), one window per Claude session named after it, the TUI in window 0.
- The daemon never owns the tmux server. If no server is running it starts one in its own scope: `systemd-run --user --scope --unit=claude-tmux tmux new-session -d -s claude`. A server started inside the daemon's cgroup would be killed, with every Claude session, on `systemctl --user restart claude-control`.
- A launched window runs `bash -li` in `cwd`; the daemon then types `claude --session-id <uuid> --name <name> "<prompt>"` into it. Claude gets the full `.bashrc` environment (nvm, pyenv, bun), and the pane is left at a shell when Claude exits.
- Focus: `tmux switch-client` on the most recently active client to the session's pane. No attached client → error. The way back to the TUI is a tmux binding the tool prints (e.g. `bind C-c switch-client -t claude:0`); it does not edit `~/.tmux.conf`.

## MCP vocabulary (v1)

| Tool | Does |
| --- | --- |
| `list_sessions(states?)` | Sessions with state, reason, last event, age, cwd, name |
| `get_session(session_id)` | One session: last assistant message or notification text, processes, panes, tool counter, current tool |
| `focus_session(session_id)` | Switches the most recent tmux client to the session's pane |
| `launch_session(cwd, prompt?, name?)` | New window in the `claude` tmux session; returns the pre-assigned `session_id` in state `starting`. `cwd` must exist; `name` defaults to the cwd basename plus a short suffix. No free-form CLI arguments. |

Resource: `sessions://` (subscribable; the TUI uses it for live updates).

## TUI

`claude-control tui`: inside tmux it runs the TUI; outside tmux it asks the daemon to ensure the `claude` tmux session exists, then execs `tmux attach -t claude`.

Rows in fixed order by start time (never re-sorted by urgency): name, shortened cwd, state badge with reason, last event in words, age, activity spinner. Keys: `j`/`k` move, `enter` focus, `n` new session (cwd with path completion, multi-line prompt), `/` filter, `e` toggle ended sessions (hidden by default).

## Milestones

1. Hook intake, raw event log, `install-hooks`, `install-service`. Collect real payloads covering `/clear`, concurrent resume, permission prompts and kill -9.
2. State machine, test-first against those payloads; reconciliation.
3. MCP tools and resource.
4. TUI list and focus.
5. Launch.

## Observed hook behaviour (Claude Code 2.1.309, 2026-10-23)

From a test session in tmux with hooks pointed at a dev daemon:

- `X-Tmux-Pane` resolves to the pane id (`%0`) on every HTTP hook. Requests arrive with `content-type: application/json` and `host: 127.0.0.1:<port>`.
- `/clear` changes `session_id`: `SessionEnd` (reason `clear`) on the old id, then `SessionStart` (source `clear`) on a new id, same pane and process.
- Permission flow: `PreToolUse` → `PermissionRequest` (same second) → `Notification` `permission_prompt` about 6s later → `PostToolUse` after approval. `PermissionRequest` is the prompt signal; `current_tool` is already set while waiting.
- Subagent and background-agent tool calls fire `PreToolUse`/`PostToolUse` on the parent `session_id` with `agent_id` set, including after `Stop`. One such `PreToolUse` had no matching `PostToolUse` before `SessionEnd`.
- `X-Claude-Pid` from the SessionStart command hook equals the claude process pid.
