"""Internal HTTP API for the Telegram bot service.

Exposes a minimal FastAPI application on :8002 that allows other services
(e.g. paper_ingestion) to trigger scheduler reloads without restarting the bot.
"""

import logging
import os

import uvicorn
from fastapi import Depends, FastAPI
from jarvis_common.auth import verify_api_key

logger = logging.getLogger(__name__)

_internal_app = FastAPI(title="JARVIS Telegram Bot Internal API", docs_url=None, redoc_url=None)


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
    dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"
    api_key = os.getenv("JARVIS_API_KEY", "")
    if dev_mode and not api_key:
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
    server = uvicorn.Server(config)
    # serve() runs until the server shuts down; we run it as a background task
    await server.serve()
