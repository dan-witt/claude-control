import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import reconcile
from .events import Event
from .hooks_install import TIMEOUT_S
from .state import Agent, Tracker
from .store import EventStore

log = logging.getLogger(__name__)

LOCAL_HOSTS = ["127.0.0.1", "localhost"]
PRUNE_INTERVAL_S = 3600
# Claude Code reuses idle connections; uvicorn closes them after 5s idle. A hook sent as the
# server closes one fails with "socket hang up" and the event is lost. One connection per hook.
CLOSE = {"connection": "close"}

# Returns the `claude agents` rows and a pid -> pane map, or None when listing failed.
Listing = Callable[[], Awaitable[tuple[list[Agent], dict[int, str]] | None]]


def system_listing(claude_bin: str) -> Listing:
    async def listing() -> tuple[list[Agent], dict[int, str]] | None:
        agents = await reconcile.list_agents(claude_bin)
        if agents is None:
            return None
        panes = reconcile.panes_for([a.pid for a in agents], await reconcile.pane_pids())
        return agents, panes

    return listing


def create_app(
    store: EventStore,
    tracker: Tracker | None = None,
    retention_days: int = 14,
    replay_hours: float = 24,
    listing: Listing | None = None,
    reconcile_interval_s: float = 15,
    slow_after_s: float = TIMEOUT_S,
) -> Starlette:
    tracker = tracker or Tracker()

    async def hooks(request: Request) -> Response:
        started = time.monotonic()
        # Requiring JSON forces a CORS preflight, so a web page cannot post forged events.
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            return PlainTextResponse("expected application/json", status_code=415, headers=CLOSE)
        body = (await request.body()).decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return PlainTextResponse("invalid JSON", status_code=400, headers=CLOSE)
        if not isinstance(data, dict):
            return PlainTextResponse("expected a JSON object", status_code=400, headers=CLOSE)
        now = time.time()
        headers = dict(request.headers)
        store.append(
            payload=body,
            session_id=data.get("session_id"),
            event=data.get("hook_event_name"),
            tmux_pane=request.headers.get("x-tmux-pane") or None,
            headers=headers,
            received_at=now,
        )
        event = Event.parse(now, data, headers)
        if event is not None:
            try:
                tracker.apply(event)
            except Exception:
                log.exception("state update failed for %s %s", event.name, event.session_id)
        elapsed = time.monotonic() - started
        if elapsed >= slow_after_s:
            # Claude Code has already given up on this request; the session waited the full timeout.
            log.warning(
                "hook %s for session %s took %.2fs (hook timeout %ss)",
                data.get("hook_event_name"),
                data.get("session_id"),
                elapsed,
                TIMEOUT_S,
            )
        # Empty 2xx: Claude Code treats it as "no decision", so tool and permission flow is unchanged.
        return Response(status_code=204, headers=CLOSE)

    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    async def sessions(request: Request) -> Response:
        include_ended = request.query_params.get("ended") in ("1", "true")
        rows = sorted(
            (s for s in tracker.sessions.values() if include_ended or s.state != "ended"),
            key=lambda s: s.started_at,
        )
        return JSONResponse([s.to_dict() for s in rows])

    def replay() -> None:
        started = time.monotonic()
        stored = store.since(time.time() - replay_hours * 3600)
        for e in stored:
            event = Event.from_stored(e)
            if event is not None:
                tracker.apply(event)
        tracker.tick(time.time())
        log.info(
            "replayed %d events into %d sessions in %.2fs",
            len(stored),
            len(tracker.sessions),
            time.monotonic() - started,
        )

    async def reconcile_once() -> None:
        assert listing is not None
        result = await listing()
        now = time.time()
        if result is not None:
            agents, panes = result
            tracker.reconcile(agents, panes, now)
        tracker.tick(now)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async def prune_loop() -> None:
            while True:
                now = time.time()
                removed = store.prune(now - retention_days * 86400)
                if removed:
                    log.info("pruned %d events", removed)
                tracker.forget_ended(now - replay_hours * 3600)
                await asyncio.sleep(PRUNE_INTERVAL_S)

        async def reconcile_loop() -> None:
            while True:
                try:
                    await reconcile_once()
                except Exception:
                    log.exception("reconciliation failed")
                await asyncio.sleep(reconcile_interval_s)

        replay()
        tasks = [asyncio.create_task(prune_loop())]
        if listing is not None:
            tasks.append(asyncio.create_task(reconcile_loop()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app = Starlette(
        routes=[
            Route("/hooks", hooks, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/sessions", sessions, methods=["GET"]),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)],
        lifespan=lifespan,
    )
    app.state.tracker = tracker
    return app
