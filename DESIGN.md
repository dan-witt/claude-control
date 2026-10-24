# claude-control design

Ticket: [ECH-17](https://linear.app/echowire/issue/ECH-17). Decided 2026-10-23.

claude-control is a systemd user service that receives Claude Code hook events, tracks every running Claude Code session on the machine, and exposes those sessions over MCP. A Textual TUI is its first MCP client: it lists sessions, focuses a session's tmux pane, and launches new sessions in tmux.

It is the session-daemon component of the [Agent Status Board design](https://claude.ai/artifact/95onJgMf4EDciR5mD3wUjn). The board, voice agent, calendar and tasks are separate components that read from this one. Differences from that doc: calendar merging is not in this daemon, `launch_session` is in the vocabulary, and `whats_blocked` is `list_sessions(states=["waiting"])`.

## Scope

In: hook intake, session state, the MCP server, the TUI.
Out: calendar, tasks, the board UI, voice. No tool sends input to a session (no messaging socket, no `tmux send-keys` into Claude); a session is answered in its own terminal.

## Runtime

- Python + uv. One Starlette app serves `/hooks` and `/mcp` (streamable HTTP, official `mcp` SDK 2.x, `MCPServer`) on `127.0.0.1:7777` (configurable). TUI in Textual.
- Packaged as `claude-control`, installed with `uv tool install`. Subcommands: `serve`, `tui`, `install-hooks`, `install-service`, `install-mcp`, `sessions`, `events`. A new dependency needs `uv tool install --editable . --force`; the service fails to start without it.
- systemd **user** unit written by `install-service` to `~/.config/systemd/user/claude-control.service`, `Restart=on-failure`, no linger. A user unit, not a system unit, because launched Claude sessions need the desktop session's environment (`SSH_AUTH_SOCK`, `DBUS_SESSION_BUS_ADDRESS`, `DISPLAY`, `GPG_AGENT_INFO`), which the user manager carries and a system service does not.
- Config: `$XDG_CONFIG_HOME/claude-control/config.toml`. State: SQLite under `$XDG_STATE_HOME/claude-control/` holding the event log; events older than 14 days are pruned. Session state lives in memory: at startup the daemon replays the last 24 hours of events (`replay_hours`), then reconciles. Ended sessions are dropped from memory after the same window.
- Tooling: pytest, ruff, pyright.

## Security

- Bind `127.0.0.1` only.
- `/mcp`: bearer token from `$XDG_CONFIG_HOME/claude-control/token` (mode 0600, generated on first run), checked by the daemon (the SDK's own auth support is OAuth-shaped); the SDK checks `Host`/`Origin` are localhost. `launch_session` runs Claude with an arbitrary prompt, so a web page reaching localhost must not be able to call it. `install-mcp` registers the endpoint with Claude Code at user scope (`claude mcp add --transport http`), which stores the token in `~/.claude.json`.
- `/hooks`: no token. Requires `Content-Type: application/json` (forces a CORS preflight) and a localhost `Host` (blocks DNS rebinding). This check may be dropped if it breaks hook delivery.
- `GET /sessions`: read-only JSON of the session state for `claude-control sessions`; localhost `Host` only, no token. Other local users can read it, as they can post to `/hooks`. The MCP resource replaces it as the interface for other components.

## Intake

Hooks are installed idempotently by `install-hooks` into `~/.claude/settings.json` (backup first; existing hooks such as peon-ping are left in place). All post the event JSON to `http://127.0.0.1:7777/hooks`:

- `UserPromptSubmit`, `Notification`, `PermissionRequest`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `StopFailure`, `SessionEnd`: HTTP hooks, `timeout: 1`, headers `"X-Tmux-Pane": "$TMUX_PANE"` and `"X-Claude-Socket": "$CLAUDE_CODE_MESSAGING_SOCKET"` (both in `allowedEnvVars`).
- `SessionStart`: a command hook running `curl`, because SessionStart accepts only `command` and `mcp_tool` hooks. It sends `X-Tmux-Pane`, `X-Claude-Socket` and `X-Claude-Pid: $PPID` (the claude process), and ends in `|| true` so a stopped daemon never fails the hook.

`X-Claude-Socket` is the claude process's messaging socket path, unique per process. `SessionStart` pairs it with the pid; later events identify their process by it.

A stopped daemon costs sessions nothing (connection refused is immediate; the UI shows a non-blocking "hook error"). A daemon that accepts connections but does not answer costs up to `timeout` per blocking hook: measured +7–8s on a short turn with one tool call at `timeout: 2`, hence `timeout: 1`. The request path must never block (no subprocess or slow I/O on the event loop). The daemon logs a warning when handling a `/hooks` request takes `timeout` or longer; time spent before the handler runs (a stalled event loop) is not measured.

`/hooks` responses carry `Connection: close`. Claude Code reuses idle connections and uvicorn closes them after 5s idle; a hook sent as the server closes its connection fails with `socket hang up` and the event is lost (observed once, on a `Stop`).

Any single event can still be lost (daemon stopped or restarting). State corrects itself on the session's next event; reconciliation catches a missed turn end only when the process reports `idle`.

Running sessions pick up settings changes without a restart.

Reconciliation: at startup and every 15s the daemon runs `claude agents --json` (documented CLI; ~0.12s wall, ~0.14s CPU per call). The underlying `~/.claude/sessions/*.json` registry is undocumented and not read directly; it keeps entries for killed processes. Reconciliation:
- discovers sessions started before the daemon (they post nothing until their next prompt);
- marks a process ended when its pid disappears (kill -9, a closed terminal that sent no `SessionEnd`);
- reads each process's `status`: `busy` (a turn or background work: forks, background shells), `waiting` (a permission or question prompt is on screen) or `idle` (none of these). This is the only prompt-free signal that a turn ended by interrupt or permission denial, which fire no hook. While background work keeps a process `busy`, a missed turn end is not detectable this way. Status rules apply only to sessions whose last main-agent event is older than 15s, because the listing can lag hooks:
  - `idle`: a `running` session, or one `waiting` for permission or input, becomes `waiting/done`; open background tasks are closed.
  - `waiting`: a `running` session becomes `waiting/permission` (the `PermissionRequest` was missed).
  - `busy`: no change.
- creates sessions it has not seen (`busy` → `running`, `waiting` → `waiting/permission`, `idle` → `idle`) and revives an ended session that is listed again; a pid listed under another session moves there;
- ends a session when none of its processes is listed. A process first seen less than 15s ago and never listed is kept, since a new process can post `SessionStart` before it appears;
- finds the tmux pane of a discovered process: walk its parent pids up to one that is a pane's `#{pane_pid}` in `tmux list-panes -a`. This covers both `claude` started from a pane's shell and `claude` as the pane's own process. A chain that reaches pid 1 without a match is a session outside tmux.

Background (`--bg`) sessions are listed, view-only.

## Session model

- Primary key: `session_id`. Each session holds a set of attached processes `{socket, pid, tmux_pane}`; in-memory maps `socket → session_id`, `pid → session_id` and `pane → session_id` resolve lookups.
- One `session_id` can have several processes: `claude --resume` of the same session in two terminals runs two pids with one id. `focus_session` picks the process with the most recent hook event.
- `/clear` and in-session `/resume`: the old `session_id` gets `SessionEnd` (reason `clear` / `resume`) and the same process starts a new or other `session_id`; the process moves to it.
- Background tasks belong to the session that launched them and do not have their own sessions. A task opens on the `PostToolUse` that launched it: `Agent` with `tool_response.status: "async_launched"` (id `tool_response.agentId`), or `Bash` with `tool_response.backgroundTaskId`. It closes on a `UserPromptSubmit` whose `prompt` is a `<task-notification>` with that `task-id`. Tool events from a fork carry its id as `agent_id` (plus `agent_type: fork`) under the parent's `session_id` and socket. A fork the user starts from the UI has no launching `PostToolUse`; it is known from its first event with an unseen `agent_id`.

States follow the main agent only; events with an `agent_id` update background activity and never change the state.

| State | Set by |
| --- | --- |
| `starting` | `launch_session`, until the session's first hook event. After 15s without one, the daemon looks for a process started with `--session-id <id>`: running → `waiting/input` (last event `waiting at startup`; the folder-trust prompt blocks `SessionStart`, and `claude agents` does not list the process until it is answered); not running → `ended/launch_failed`. Reconciliation does not end a launched session before that check. |
| `idle` (new) | `SessionStart` of a new session, which sits at an empty prompt; `compact` and a second `resume` of a live session keep the state |
| `running` | `UserPromptSubmit` (typed, queued mid-turn, or a `<task-notification>`; no `Stop` need precede it), or main-agent tool activity after a wait |
| `waiting` | reason `permission` (`PermissionRequest`, including `ExitPlanMode`), `input` (`PermissionRequest` for `AskUserQuestion`, `Notification` `agent_needs_input` / `elicitation_dialog`), `done` (`Stop`). `Notification` `permission_prompt` changes nothing. `Notification` `idle_prompt` is not sent while a prompt is open, so it moves `running` (interrupted turn) and `waiting/permission`/`input` (dismissed prompt) to `waiting/done` and clears open tool calls. |
| `idle` | `waiting/done` with no event for 10 min |
| `error` | `StopFailure` |
| `ended` | `SessionEnd`, or pid gone at reconciliation |

A `PermissionRequest` can come from a hook-driven MCP call with nothing on screen; it stays `waiting/permission` until the next main-agent event, `idle_prompt`, or reconciliation sees `idle`.

The first event of a session the daemon has not seen (started before the daemon, or older than the replay window) creates it as `running`, unless the event sets another state.

Processes are matched to events by socket, then pid, then pane; a process known only by pid (from reconciliation) is taken over by the first event carrying a socket.

Activity: main-agent tool calls are tracked as a set of open `tool_use_id`s (`PreToolUse` adds, `PostToolUse`/`PostToolUseFailure` removes) plus a counter. Parallel calls overlap and finish out of order. `current_tool` is the most recently opened call still open. Background activity is the set of open background tasks and the time of the last event from each fork. The TUI shows a spinner while a main-agent call is open or the counter changed in the last few seconds, and a separate background indicator while tasks are open. `PreToolUse` for a call can precede its execution by up to a minute when the model emits several calls in one response; the spinner shows activity during that time, when the model is generating.

## tmux

- tmux is the session host. Sessions outside tmux are tracked and listed but cannot be focused.
- Launched sessions go into a dedicated tmux session `claude`, one window per Claude session named after it, the TUI in a window named `tui`.
- The daemon never owns the tmux server. Whoever starts the server (the `tui` command now, the daemon for `launch_session`) starts it in its own scope: `systemd-run --user --scope --unit=claude-tmux tmux new-session -d -s claude ...`. A server started inside the daemon's cgroup would be killed, with every Claude session, on `systemctl --user restart claude-control`. Verified: the server's cgroup is `app.slice/claude-tmux.scope`, and the scope goes away with the server.
- A launched window (`new-window -d`, so no client moves to it) runs `bash -li` in `cwd`; the daemon registers the session as `starting`, then types `claude --session-id <uuid> --name <name> [launch_args] $'<prompt>'` into it. The prompt is quoted as one bash `$'...'` word, so a multi-line prompt types as a single line. Claude gets the full `.bashrc` environment (nvm, pyenv, bun), and the pane is left at a shell when Claude exits. `launch_args` (config) adds fixed arguments such as a model; the tool takes no free-form arguments.
- Focus: `tmux switch-client` on the most recently active client to the session's pane. No attached client → error. With tmux 3.4, `switch-client -c <client> -t <pane id>` selects the pane's session, window and pane in one step. The way back to the TUI is a tmux binding documented in the README (`bind C-c switch-client -t claude:tui`); the tool does not edit `~/.tmux.conf`.

## MCP vocabulary (v1)

| Tool | Does |
| --- | --- |
| `list_sessions(states?)` | Sessions oldest first: `session_id`, `name`, `state`, `reason`, `cwd`, `last_event`, `last_event_age_s`, `started_at`, `pane`, `current_tool`, `tool_count`, `background_tasks`. Without `states`, every session that has not ended; an unknown state name is an error. |
| `get_session(session_id)` | One session: the list fields plus `message` (last assistant message or notification text), `permission_tool`, `processes` (pid, pane), `tasks` (open background tasks), `ended_at` |
| `focus_session(session_id)` | `tmux switch-client -c <client> -t <pane>` on the client with the latest `client_activity`, to the pane of the session's most recently active process. Errors: session ended, not in tmux, no client attached. |
| `launch_session(cwd, prompt?, name?)` | New window in the `claude` tmux session (created, with its server in its own scope, if missing); returns `session_id`, `name`, `pane` and `state: starting`. `cwd` must be an existing absolute directory (`~` expands); `name` defaults to the cwd basename plus two hex digits and must not match a live session. No free-form CLI arguments. |

`session_id` arguments accept the full id, a session's name (from `claude agents`), or a unique id prefix of at least 4 characters.

Resource: `sessions://`, JSON of `list_sessions()`. Subscribers are notified (`ResourceUpdated`) when a session is added or removed or its state, reason, name or processes change; tool activity does not notify. Notifications use the 2026-07-28 `subscriptions/listen` stream; clients on earlier protocol versions (`resources/subscribe`) can read the resource but get no notifications.

## TUI

`claude-control tui`: inside tmux (or with `--here`) it runs the TUI; outside tmux it makes sure the `claude` tmux session has a `tui` window running `claude-control tui` (starting the server in its own scope, as above), then execs `tmux attach -t claude:tui`. The command does this itself rather than through the daemon: `systemd-run --scope` gives the same isolation from any process, and the daemon needs no extra endpoint.

The TUI is an MCP client of the daemon (`list_sessions`, `focus_session`). It refreshes on `sessions://` notifications and polls every second for tool activity and ages; when the daemon is unreachable it shows one notification and retries.

Rows in fixed order by start time (never re-sorted by urgency): activity spinner (a main-agent tool call is open, or `tool_count` changed in the last 3s), name, shortened cwd, state badge with reason, last event in words, age of the last event. Keys: `j`/`k` move, `enter` focus, `/` filter (name, directory, state; `escape` clears), `e` toggle ended sessions (hidden by default), `n` new session, `q` quit. The new-session dialog has a directory field (default: the selected session's directory; `→` accepts a directory completion), an optional name and a multi-line prompt; `ctrl+s` launches, `escape` cancels, and a launch error stays in the dialog.

## Milestones

1. Hook intake, raw event log, `install-hooks`, `install-service`. Collect real payloads covering `/clear`, concurrent resume, permission prompts and kill -9.
2. State machine, test-first against those payloads; reconciliation.
3. MCP tools and resource.
4. TUI list and focus.
5. Launch.

## Observed hook behaviour (Claude Code 2.1.309–2.1.310, 2026-10-23)

From test sessions in a separate tmux server (Haiku) with every documented hook event pointed at a dev daemon, plus real sessions on the live daemon (the fork rows are from a real session running six forks). Raw payloads from the test runs are not kept; repeat with the harness described in `CLAUDE.md`.

| Case | Events observed | Consequence for the state machine |
| --- | --- | --- |
| Startup, prompt on the command line | `SessionStart` (source `startup`), `UserPromptSubmit` ~1.3s later | `claude "<prompt>"` submits the prompt; launch can rely on it |
| Plain turn | `UserPromptSubmit` → `Stop`; often a `SubagentStop` with `agent_id` ~1s after `Stop` | Background agents keep posting after `Stop` |
| Tool call | `PreToolUse` → `PostToolUse` → `PostToolBatch` | |
| Bash exits non-zero | `PostToolUse` (not a failure) | |
| Tool error (Read of a missing file) | `PreToolUse` → `PostToolUseFailure` | `PostToolUseFailure` must clear `current_tool` |
| Three parallel Reads | Three sequential `Pre`/`Post` pairs, then one `PostToolBatch` | Pairs did not overlap |
| Four parallel Bash calls | Four `PreToolUse`, then four `PostToolUse` in a different order | Open calls are a set keyed by `tool_use_id` |
| Six `Agent` (fork) calls in one response | `PreToolUse` for each as the model emitted it, spread over 62s; all six `PostToolUse` together at launch with `status: async_launched`; first fork tool call 5s later | `PostToolUse` means launched, not finished |
| Forks running | Tool events with `agent_id`, `agent_type: fork`, parent `session_id`, `prompt_id` and socket; overlapping each other and the main agent; up to 136s before a fork's first tool call | Background activity, not session state |
| Fork the user starts from the UI | No `Agent` tool event; first sign is a tool event with a new `agent_id` (a slug of its prompt) | |
| Fork or background shell finishes | `UserPromptSubmit` with `prompt` `<task-notification>` carrying `task-id` (= `agentId` / `backgroundTaskId`) and `tool-use-id`; a new turn if the main agent was idle, otherwise delivered between its tool calls with no `Stop` before it | Closes the background task |
| Message typed while a turn runs | `UserPromptSubmit` when delivered between tool calls; no `Stop` before it | |
| Main agent stopped, background work running | `claude agents` reports `busy`; observed >5 min after the final `Stop` with a background build running | Reconciliation cannot detect a missed turn end then |
| Tool call approved by auto mode | `PreToolUse` → `PostToolUse`, no `PermissionRequest` (94 calls) | `PermissionRequest` means a prompt (or a hook-driven MCP call) |
| Permission prompt answered within ~6s | No `permission_prompt` (answered at 5.2s and 6.0s) | |
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
| Keep-alive race | A `Stop` sent 5.0s after the previous request on the same connection failed with `socket hang up` (uvicorn's idle timeout is 5s); the next request opened a new connection | Fixed by `Connection: close` |
| Permission prompt left open (Haiku, `--permission-mode default`) | `claude agents` `status: waiting` for the whole 92s; no `idle_prompt`. After Esc: `status: idle` within 5s, `idle_prompt` 61s later | `waiting` and `idle` statuses drive reconciliation |
| `--bg` session row | `claude agents` rows for `kind: background` also carry `id` and `state` (`blocked` observed while `status: idle`) | Not used |
| Pane lookup for a discovered pid | Parent-pid walk to `#{pane_pid}` matched the `X-Tmux-Pane` from `SessionStart` for `claude` started from a pane's shell and for `claude` as the pane process | |

Request details: `content-type: application/json`, `host: 127.0.0.1:<port>`, `user-agent: axios/…`. `X-Tmux-Pane` resolves on every hook; `X-Claude-Pid` equals the claude pid; `claude agents --json` names are per process and change on resume (`live-b0` → `live-b4`, `live-8c`).
