"""API key authentication shared across JARVIS services."""

import hmac
import logging
import os

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_HEALTH_PATHS = frozenset({"/health", "/health/", "/healthz", "/health/readiness"})


async def verify_api_key(
    request: Request, api_key: str | None = Depends(_api_key_header)
) -> None:
    """Validate API key. Requires JARVIS_API_KEY unless DEV_MODE=true."""
    jarvis_api_key = os.environ.get("JARVIS_API_KEY", "")
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    if request.url.path in _HEALTH_PATHS:
        return
    if not jarvis_api_key:
        if dev_mode:
            logger.warning(
                "DEV_MODE=true — ALL authentication bypassed on %s. "
                "DO NOT USE IN PRODUCTION.",
                request.url.path,
            )
            return
        raise HTTPException(
            status_code=401,
            detail="API key not configured. Set JARVIS_API_KEY or enable DEV_MODE.",
        )
    if not hmac.compare_digest(api_key or "", jarvis_api_key):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


def validate_production_config() -> None:
    """Crash at startup if production config is unsafe.

    Raises
    ------
    RuntimeError
        If ENVIRONMENT=production AND DEV_MODE=true, or if not in DEV_MODE
        and JARVIS_API_KEY is empty, a default sentinel, or shorter than 32 chars.
    """
    env = os.environ.get("ENVIRONMENT", "").lower()
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    api_key = os.environ.get("JARVIS_API_KEY", "")

    if env == "production" and dev_mode:
        raise RuntimeError("DEV_MODE=true is not allowed in ENVIRONMENT=production")

    if not dev_mode:
        if not api_key or api_key == "CHANGE_ME_REQUIRED":
            raise RuntimeError(
                "JARVIS_API_KEY must be set to a real value (not empty or default sentinel)"
            )
        if len(api_key) < 32:
            raise RuntimeError(
                f"JARVIS_API_KEY must be at least 32 characters (got {len(api_key)})"
            )
