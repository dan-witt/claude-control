# claude-control

Tracks running Claude Code sessions from their hook events and exposes them over MCP. See `DESIGN.md`.

```sh
uv tool install --editable .
claude-control install-service   # systemd user unit on 127.0.0.1:7777
claude-control install-hooks     # adds hooks to ~/.claude/settings.json (backup kept)
claude-control install-mcp       # registers the MCP endpoint with Claude Code (user scope)
claude-control events            # recent hook events
claude-control sessions          # session state as the daemon sees it
```

Remove the hooks with `claude-control install-hooks --uninstall`.
