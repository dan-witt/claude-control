import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .hooks_install import TIMEOUT_S
from .store import EventStore

log = logging.getLogger(__name__)

LOCAL_HOSTS = ["127.0.0.1", "localhost"]
PRUNE_INTERVAL_S = 3600
# Claude Code reuses idle connections; uvicorn closes them after 5s idle. A hook sent as the
# server closes one fails with "socket hang up" and the event is lost. One connection per hook.
CLOSE = {"connection": "close"}


def create_app(
    store: EventStore, retention_days: int = 14, slow_after_s: float = TIMEOUT_S
) -> Starlette:
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
        store.append(
            payload=body,
            session_id=data.get("session_id"),
            event=data.get("hook_event_name"),
            tmux_pane=request.headers.get("x-tmux-pane") or None,
            headers=dict(request.headers),
        )
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

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async def prune_loop() -> None:
            while True:
                removed = store.prune(time.time() - retention_days * 86400)
                if removed:
                    log.info("pruned %d events", removed)
                await asyncio.sleep(PRUNE_INTERVAL_S)

        task = asyncio.create_task(prune_loop())
        try:
            yield
        finally:
            task.cancel()

    return Starlette(
        routes=[
            Route("/hooks", hooks, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)],
        lifespan=lifespan,
    )
