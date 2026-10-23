import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    received_at REAL NOT NULL,
    session_id TEXT,
    event TEXT,
    tmux_pane TEXT,
    headers TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events (session_id, received_at);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at);
"""


@dataclass(frozen=True)
class Event:
    id: int
    received_at: float
    session_id: str | None
    event: str | None
    tmux_pane: str | None
    headers: dict[str, str]
    payload: str


class EventStore:
    """Append-only log of raw hook payloads, as received."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def append(
        self,
        payload: str,
        session_id: str | None,
        event: str | None,
        tmux_pane: str | None,
        headers: dict[str, str],
        received_at: float | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO events (received_at, session_id, event, tmux_pane, headers, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                time.time() if received_at is None else received_at,
                session_id,
                event,
                tmux_pane,
                json.dumps(headers),
                payload,
            ),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def prune(self, older_than: float) -> int:
        return self._db.execute("DELETE FROM events WHERE received_at < ?", (older_than,)).rowcount

    def recent(self, limit: int = 50, session_id: str | None = None) -> list[Event]:
        sql = "SELECT id, received_at, session_id, event, tmux_pane, headers, payload FROM events"
        args: tuple = ()
        if session_id:
            sql += " WHERE session_id = ?"
            args = (session_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._db.execute(sql, (*args, limit)).fetchall()
        return [Event(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6]) for r in reversed(rows)]

    def close(self) -> None:
        self._db.close()
