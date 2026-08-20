"""Private liveness API for the database-free Telegram adapter."""

import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from jarvis_common import create_limiter
from jarvis_common.app_factory import configure_middleware_and_errors
from jarvis_common.health import register_health_routes
from jarvis_common.settings import get_core_settings
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
    task: asyncio.Task[None] | None = None


_server_state = _ServerState()


register_health_routes(
    _internal_app,
    service_name="telegram_bot",
    checks=[],
    limiter=limiter,
)


async def start_internal_server(port: int = 8002) -> None:
    """Start the internal uvicorn server as an asyncio task.

    Parameters
    ----------
    port : int
        TCP port to listen on (default 8002).
    """
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

    def _on_done(task: asyncio.Task[None]) -> None:
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
