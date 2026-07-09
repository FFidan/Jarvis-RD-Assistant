"""Internal HTTP API for the Telegram bot service.

Exposes a minimal FastAPI application on :8002 that allows other services
(e.g. paper_ingestion) to trigger scheduler reloads without restarting the bot.
"""

import asyncio
import logging

import uvicorn
from fastapi import Depends, FastAPI
from jarvis_common import create_limiter
from jarvis_common.app_factory import configure_middleware_and_errors
from jarvis_common.auth import verify_api_key
from jarvis_common.settings import get_core_settings, get_secrets_settings
from jarvis_common.version import app_version

limiter = create_limiter(default_limits=["600/minute"], user_aware=False)

logger = logging.getLogger(__name__)

_internal_app = FastAPI(
    title="JARVIS Telegram Bot Internal API",
    version=app_version(),
    docs_url=None,
    redoc_url=None,
)

configure_middleware_and_errors(
    _internal_app,
    limiter=limiter,
    trusted_proxy_hosts=get_core_settings().trusted_proxy_hosts_list,
)

from jarvis_common.session_middleware import SessionMiddleware  # noqa: E402

_internal_app.add_middleware(SessionMiddleware)


class _ServerState:
    """Holds uvicorn server handles set by :func:`start_internal_server`.

    Replaces module-level ``global`` mutation so callers can access and cancel
    the server without relying on ``global`` keyword side-effects.
    """

    server: uvicorn.Server | None = None
    task: asyncio.Task | None = None  # type: ignore[type-arg]


_server_state = _ServerState()


@_internal_app.get("/health")
async def health() -> dict[str, str]:
    """Health check — no authentication required."""
    return {"status": "ok"}


@_internal_app.post("/internal/reload-nudges", dependencies=[Depends(verify_api_key)])
async def reload_nudges() -> dict[str, str]:
    """Re-register all nudge_* scheduler jobs from the database.

    Reads the current user.timezone and re-schedules every enabled nudge.
    Called by paper_ingestion whenever a nudge or user.timezone config changes.
    """
    scheduler = _internal_app.state.scheduler  # type: ignore[attr-defined]
    await scheduler.reload_nudges()
    logger.info("reload-nudges completed via internal API")
    return {"status": "ok"}


async def start_internal_server(scheduler: object, port: int = 8002) -> None:
    """Start the internal uvicorn server as an asyncio task.

    Parameters
    ----------
    scheduler:
        The :class:`~telegram_bot.scheduler.JarvisScheduler` instance to attach.
    port:
        TCP port to listen on (default 8002).
    """
    # F-01: Refuse to start unauthenticated internal API in DEV_MODE
    core = get_core_settings()
    api_key_secret = get_secrets_settings().jarvis_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret is not None else ""
    if core.dev_mode and not api_key:
        logger.warning(
            "Refusing to start telegram_bot internal API: DEV_MODE=true and "
            "JARVIS_API_KEY is empty — unauthenticated endpoint would accept any caller."
        )
        return

    _internal_app.state.scheduler = scheduler  # type: ignore[attr-defined]
    # H.9: bind to 0.0.0.0 inside the container so the paper_ingestion sibling
    # service can reach this endpoint over the Docker bridge network at
    # http://telegram_bot:8002. The telegram_bot service does NOT publish
    # this port to the host (no `ports:` block in docker-compose.yml), so
    # reachability is limited to the internal Docker network. The endpoint
    # is additionally protected by ``verify_api_key`` (X-API-Key check) and
    # the F-01 startup guard above refuses to bind at all when DEV_MODE=true
    # and JARVIS_API_KEY is empty. If the deployment posture ever changes
    # to host-network or to a config where host port 8002 is published,
    # tighten this to 127.0.0.1 and route reload-nudges via a UNIX socket
    # or an external load balancer instead.
    config = uvicorn.Config(
        _internal_app,
        host="0.0.0.0",  # noqa: S104 — see comment above
        port=port,
        log_level="warning",
        # Reuse the running asyncio event loop managed by PTB
        loop="none",
    )
    _server_state.server = uvicorn.Server(config)

    # Capture this task's handle so post_shutdown can cancel/await it
    _server_state.task = asyncio.current_task()

    def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Internal API server task exited unexpectedly: %s",
                    exc,
                    exc_info=exc,
                )

    if _server_state.task is not None:
        _server_state.task.add_done_callback(_on_done)

    # serve() blocks until the server shuts down
    if _server_state.server is None:
        return
    await _server_state.server.serve()
