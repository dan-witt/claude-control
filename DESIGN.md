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

- `UserPromptSubmit`, `Notification`, `PermissionRequest`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `StopFailure`, `SessionEnd`: HTTP hooks, `timeout: 1`, headers `"X-Tmux-Pane": "$TMUX_PANE"` and `"X-Claude-Socket": "$CLAUDE_CODE_MESSAGING_SOCKET"` (both in `allowedEnvVars`).
- `SessionStart`: a command hook running `curl`, because SessionStart accepts only `command` and `mcp_tool` hooks. It sends `X-Tmux-Pane`, `X-Claude-Socket` and `X-Claude-Pid: $PPID` (the claude process), and ends in `|| true` so a stopped daemon never fails the hook.

`X-Claude-Socket` is the claude process's messaging socket path, unique per process. `SessionStart` pairs it with the pid; later events identify their process by it.

A stopped daemon costs sessions nothing (connection refused is immediate; the UI shows a non-blocking "hook error"). A daemon that accepts connections but does not answer costs up to `timeout` per blocking hook: measured +7–8s on a short turn with one tool call at `timeout: 2`, hence `timeout: 1`. The request path must never block (no subprocess or slow I/O on the event loop). The daemon logs a warning when handling a `/hooks` request takes `timeout` or longer; time spent before the handler runs (a stalled event loop) is not measured.

Running sessions pick up settings changes without a restart.

Reconciliation: at startup and every 15s the daemon runs `claude agents --json` (documented CLI; ~0.12s wall, ~0.14s CPU per call). The underlying `~/.claude/sessions/*.json` registry is undocumented and not read directly; it keeps entries for killed processes. Reconciliation:
- discovers sessions started before the daemon (they post nothing until their next prompt);
- marks a process ended when its pid disappears (kill -9, a closed terminal that sent no `SessionEnd`);
- reads each process's `status` (`busy`/`idle`). This is the only signal that a turn ended by interrupt or permission denial, which fire no hook.

Background (`--bg`) sessions are listed, view-only.

## Session model

- Primary key: `session_id`. Each session holds a set of attached processes `{socket, pid, tmux_pane}`; in-memory maps `socket → session_id`, `pid → session_id` and `pane → session_id` resolve lookups.
- One `session_id` can have several processes: `claude --resume` of the same session in two terminals runs two pids with one id. `focus_session` picks the process with the most recent hook event.
- `/clear` and in-session `/resume`: the old `session_id` gets `SessionEnd` (reason `clear` / `resume`) and the same process starts a new or other `session_id`; the process moves to it.

States (stable names; may be revised after milestone 1 discovery):

| State | Set by |
| --- | --- |
| `starting` | `launch_session`, until `SessionStart` arrives; `error` with reason `launch_timeout` after ~15s |
| `running` | `UserPromptSubmit`, or tool activity after a wait |
| `waiting` | reason `permission` (`PermissionRequest`, `Notification` `permission_prompt`), `input` (`PermissionRequest` for `AskUserQuestion`, `Notification` `idle_prompt` / `agent_needs_input` / elicitation), `done` (`Stop`) |
| `idle` | `waiting/done` with no event for 10 min |
| `error` | `StopFailure` |
| `ended` | `SessionEnd`, or pid gone at reconciliation |

Activity: `PreToolUse` sets `current_tool` and increments a tool counter; `PostToolUse` or `PostToolUseFailure` clears `current_tool` and increments it. The TUI shows a spinner while `current_tool` is set or the counter changed in the last few seconds.

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

From test sessions in a separate tmux server (Haiku) with every documented hook event pointed at a dev daemon, plus real sessions on the live daemon. Raw payloads from the test runs are not kept; repeat with the harness described in `CLAUDE.md`.

| Case | Events observed | Consequence for the state machine |
| --- | --- | --- |
| Startup, prompt on the command line | `SessionStart` (source `startup`), `UserPromptSubmit` ~1.3s later | `claude "<prompt>"` submits the prompt; launch can rely on it |
| Plain turn | `UserPromptSubmit` → `Stop`; often a `SubagentStop` with `agent_id` ~1s after `Stop` | Background agents keep posting after `Stop` |
| Tool call | `PreToolUse` → `PostToolUse` → `PostToolBatch` | |
| Bash exits non-zero | `PostToolUse` (not a failure) | |
| Tool error (Read of a missing file) | `PreToolUse` → `PostToolUseFailure` | `PostToolUseFailure` must clear `current_tool` |
| Three parallel Reads | Three sequential `Pre`/`Post` pairs, then one `PostToolBatch` | Pairs did not overlap |
| Permission prompt | `PreToolUse` → `PermissionRequest` (same 20ms) → `Notification` `permission_prompt` ~6s later → `PostToolUse` on approval | `PermissionRequest` is the prompt signal; `current_tool` is set while waiting |
| `AskUserQuestion` | `PreToolUse` → `PermissionRequest` with `tool_name: AskUserQuestion` → `permission_prompt` ~6s later → `PostToolUse` on answer | Reason `input`, distinguished by tool name |
| Plan approval | `PreToolUse` → `PermissionRequest` with `tool_name: ExitPlanMode` → `permission_prompt` ~6s later | Reason `permission` (or a distinct `plan`) by tool name |
| Esc while a tool runs | `PreToolUse`, then nothing: no `Post*`, no `Stop`, no `idle_prompt` after 11 min | Only reconciliation `status: idle` shows the turn ended |
| Esc or "No" at a permission prompt | `PreToolUse` → `PermissionRequest`, then nothing; `idle_prompt` ~61s later | Same; `idle_prompt` arrives in this case only |
| Turn finished, no input | `Notification` `idle_prompt` 61s after `Stop` | |
| API error (stub returning 400) | `UserPromptSubmit` → `StopFailure` (`error: "unknown"`, `last_assistant_message: "API Error: 400 …"`); no `Stop` | `error` state |
| `/clear` | `SessionEnd` (reason `clear`) old id → `SessionStart` (source `clear`) new id; same pid, pane, socket | |
| `/compact` | `PreCompact` → `SessionStart` (source `compact`, same id) → `PostCompact`, ~10s | No `UserPromptSubmit`/`Stop` around it |
| `/resume <id>` inside a session | `SessionEnd` (reason `resume`) old id → `SessionStart` (source `resume`) on `<id>`; same pid, pane | The target id can now have two processes |
| `--resume <id> --fork-session` | `SessionStart` (source `fork`) on a new id | |
| `--resume` of one session in two terminals | Two `SessionStart` (source `resume`), same id, different `X-Claude-Pid` and socket | Per-process state needs the socket header |
| `-p` | `SessionStart`/`SessionEnd` and tool events with pid and socket, no pane; listed by `claude agents --json` as `interactive` while running | Tracked like any session; reconciliation does not drop it early |
| `--bg` | Same as `-p`; `--settings` is honoured; `claude agents` showed `busy`/`working` after `Stop`; `claude stop` → `SessionEnd` (reason `other`) | `status` from `claude agents` can lag hooks |
| Terminal closed (`tmux kill-session`/`kill-server`) | `SessionEnd` (reason `other`) for sessions that had a turn; none for one that never submitted a prompt | Reconciliation still needed |
| kill -9 (pid 31520) | Nothing. `~/.claude/sessions/31520.json` stays on disk with `status: "idle"`; `claude agents --json` omits the pid immediately | Use the CLI, not the registry files |
| `SessionEnd` headers | `X-Claude-Socket` is empty on `SessionEnd` from exit or closed terminal (present on `/resume`) | Match `SessionEnd` by `session_id` + pane; reconciliation covers the rest |
| Hook-driven MCP calls | A plugin's hook-driven MCP call at session start fired `PreToolUse` + `PermissionRequest` (first with `prompt_id: null`) with no `Post*`, in a `-p` session | A `PermissionRequest` is not always a visible prompt |
| Blocked tool call | Claude Code's built-in guard rejected `sleep 45` before any `PreToolUse` | |
| Background task completion | Starts a new turn: `UserPromptSubmit` → `Stop` | |
| Daemon frozen (SIGSTOP) | Short turn 2.5–4s → 10.8–11.8s. Requests queued in the socket were processed on resume with that time as `received_at` | Never block the request path; `received_at` is arrival, not event time |
| Daemon stopped | No slowdown; events lost; UI shows "Stop hook error occurred" | |

Request details: `content-type: application/json`, `host: 127.0.0.1:<port>`, `user-agent: axios/…`. `X-Tmux-Pane` resolves on every hook; `X-Claude-Pid` equals the claude pid; `claude agents --json` names are per process and change on resume (`live-b0` → `live-b4`, `live-8c`).
