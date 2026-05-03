"""Internal HTTP API for the Telegram bot service.

Exposes a minimal FastAPI application on :8002 that allows other services
(e.g. paper_ingestion) to trigger scheduler reloads without restarting the bot.
"""

import asyncio
import logging

import uvicorn
from fastapi import Depends, FastAPI
from jarvis_common.auth import verify_api_key
from jarvis_common.settings import get_core_settings

logger = logging.getLogger(__name__)

_internal_app = FastAPI(title="JARVIS Telegram Bot Internal API", docs_url=None, redoc_url=None)


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
        The :class:`~app.scheduler.JarvisScheduler` instance to attach.
    port:
        TCP port to listen on (default 8002).
    """
    # F-01: Refuse to start unauthenticated internal API in DEV_MODE
    core = get_core_settings()
    if core.dev_mode and not core.jarvis_api_key:
        logger.warning(
            "Refusing to start telegram_bot internal API: DEV_MODE=true and "
            "JARVIS_API_KEY is empty — unauthenticated endpoint would accept any caller."
        )
        return

    _internal_app.state.scheduler = scheduler  # type: ignore[attr-defined]
    config = uvicorn.Config(
        _internal_app,
        host="0.0.0.0",
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
    await _server_state.server.serve()
