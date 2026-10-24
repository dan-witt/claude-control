# claude-control

A local daemon that tracks every Claude Code session on the machine from its hooks, serves those sessions over MCP, and a tmux TUI to list them, jump to one, and start new ones.

- **Session state from hooks:** every session is `running`, `waiting` (for permission, for input, or done with its turn), `idle`, `error` or `ended`, including sessions started before the daemon and sessions killed without a goodbye.
- **MCP tools** any MCP client can call: `list_sessions`, `get_session`, `focus_session`, `launch_session`, plus a `sessions://` resource that notifies subscribers when a session changes state.
- **A tmux TUI:** one row per session in a fixed order, Enter jumps to the session's pane, `n` starts a session in a new tmux window.

Design, state rules and the observed Claude Code hook behaviour they rest on: [`DESIGN.md`](DESIGN.md).

## Requirements

- Linux with a systemd user session
- tmux 3.4 or later
- Claude Code 2.1.309 or later (the version the hook behaviour was checked against)
- Python 3.12 and [uv](https://docs.astral.sh/uv/)

## Install

```sh
git clone https://github.com/dan-witt/claude-control
cd claude-control
uv tool install --editable .
claude-control install-service   # systemd user unit, listening on 127.0.0.1:7777
claude-control install-hooks     # adds hooks to ~/.claude/settings.json (keeps a backup)
claude-control install-mcp       # registers the MCP endpoint with Claude Code, user scope
```

Running sessions pick up the hooks without a restart. Sessions already running before the daemon are found within 15 seconds.

`install-mcp` creates a bearer token in `~/.config/claude-control/token` (mode 0600) and stores it in Claude Code's config with the server entry.

## Use

### TUI

```sh
claude-control tui
```

Outside tmux it opens (or reuses) a `tui` window in a tmux session named `claude` and attaches to it. Inside tmux it runs in the current pane.

| Key | Does |
| --- | --- |
| `j` / `k` | Move |
| `Enter` | Switch your tmux client to the session's pane |
| `n` | New session: directory (→ accepts a completion), optional name, optional multi-line prompt; `ctrl+s` launches |
| `/` | Filter by name, directory or state; `Esc` clears |
| `e` | Show ended sessions |
| `q` | Quit |

To get back to the list from a session's pane, add to `~/.tmux.conf`:

```
bind C-c switch-client -t claude:tui
```

### From Claude Code

After `install-mcp`, any session can use the tools, for example "which sessions are waiting on me?" or "focus the billing session".

| Tool | Does |
| --- | --- |
| `list_sessions(states?)` | Sessions with state, reason, directory, last event and its age |
| `get_session(session_id)` | One session in detail: last assistant message, processes and panes, background tasks |
| `focus_session(session_id)` | Switches the most recently used tmux client to the session's pane |
| `launch_session(cwd, prompt?, name?)` | Starts a session in a new window of the `claude` tmux session |

A `session_id` argument also accepts the session's name or a unique prefix of its id.

### CLI

```sh
claude-control sessions [-a] [--json]       # session state as the daemon sees it
claude-control events [-n N] [-s SESSION]   # raw hook events, newest last
```

## Session states

| State | Reason | Meaning |
| --- | --- | --- |
| `starting` | | Launched by `launch_session`, Claude not up yet |
| `running` | | Working on a turn |
| `waiting` | `permission` | A permission prompt or plan approval is on screen |
| `waiting` | `input` | Claude asked a question, or is stuck at a startup prompt such as folder trust |
| `waiting` | `done` | Finished its turn |
| `idle` | | Done and untouched for 10 minutes, or at an empty prompt |
| `error` | API error code | The turn ended on an API error |
| `ended` | why | The session exited, was cleared, or its process is gone |

## Configuration

Optional, in `~/.config/claude-control/config.toml`; restart with `systemctl --user restart claude-control` after a change.

```toml
port = 7777                       # then run install-hooks and install-mcp again
retention_days = 14               # how long raw hook events are kept
launch_args = ["--model", "opus"] # extra claude arguments for launch_session
```

## Security

The daemon listens on 127.0.0.1 only and checks the `Host` header. `/mcp` requires the bearer token. `/hooks` and the read-only `/sessions` endpoint need no token, so other local users on the same machine can post events and read session state.

## Uninstall

```sh
claude-control install-hooks --uninstall
claude-control install-mcp --uninstall
systemctl --user disable --now claude-control
rm ~/.config/systemd/user/claude-control.service
uv tool uninstall claude-control
```

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

`CLAUDE.md` covers the dev harness for testing against real Claude Code sessions without touching the running daemon.

## License

GPL-3.0; see [`LICENSE`](LICENSE).
