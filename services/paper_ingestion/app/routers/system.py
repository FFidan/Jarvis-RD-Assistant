"""System status endpoints used by the setup wizard."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from jarvis_common import verify_api_key
from pydantic import BaseModel

from app.deps import limiter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/system",
    tags=["system"],
    dependencies=[Depends(verify_api_key)],
)

# Ollama models expected after first-run provisioning. We match by name prefix
# so either "mistral-nemo" or "mistral-nemo:latest" counts as installed.
_EXPECTED_MODEL_PREFIXES: tuple[str, ...] = (
    "mistral-nemo",
    "qwen",
    "nomic-embed-text",
)

# TTL cache for Ollama probe: (timestamp, (models_ready, downloading))
_ollama_probe_cache: tuple[float, tuple[bool, list[str]]] | None = None


class SetupStatus(BaseModel):
    setup_completed: bool
    models_ready: bool
    models_downloading: list[str]
    topics_count: int
    telegram_configured: bool
    telegram_paired: bool


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Interpret user_config JSONB values as booleans."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", "null", ""):
            return False
    return bool(value)


def _is_owner_chat_paired(value: Any) -> bool:
    """Return True iff ``telegram.owner_chat_id`` contains a real chat id."""
    if value is None:
        return False
    if isinstance(value, str) and value.lower() == "null":
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return False


def _models_match(installed_names: list[str]) -> bool:
    """Return True iff every expected model prefix is present in installed."""
    if not installed_names:
        return False
    for expected in _EXPECTED_MODEL_PREFIXES:
        if not any(name.startswith(expected) for name in installed_names):
            return False
    return True


async def _probe_ollama() -> tuple[bool, list[str]]:
    """Probe ``{OLLAMA_BASE_URL}/api/tags``; return (models_ready, downloading).

    Results are cached for 10 seconds to avoid hammering Ollama on every
    setup-status request. Any failure (network, timeout, non-200) yields
    ``(False, [])``. The caller must never crash on this.
    """
    global _ollama_probe_cache
    now = time.monotonic()
    if _ollama_probe_cache is not None and now - _ollama_probe_cache[0] < 10:
        return _ollama_probe_cache[1]

    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
        if resp.status_code != 200:
            result: tuple[bool, list[str]] = (False, [])
            _ollama_probe_cache = (now, result)
            return result
        data = resp.json()
        installed = [m.get("name", "") for m in data.get("models", [])]
        result = (_models_match(installed), [])
    except Exception:
        logger.warning("setup-status: Ollama probe failed", exc_info=True)
        result = (False, [])
    _ollama_probe_cache = (now, result)
    return result


@router.get("/setup-status", response_model=SetupStatus)
@limiter.limit("30/minute")
async def get_setup_status(request: Request) -> SetupStatus:
    """Return a point-in-time snapshot of setup wizard readiness signals."""
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value FROM user_config WHERE key = ANY($1::text[])",
            ["setup.completed", "telegram.owner_chat_id"],
        )
        topics_row = await conn.fetchrow("SELECT COUNT(*) AS n FROM topics")

    config: dict[str, Any] = {r["key"]: r["value"] for r in rows}
    setup_completed = _coerce_bool(config.get("setup.completed"), default=False)
    telegram_paired = _is_owner_chat_paired(config.get("telegram.owner_chat_id"))
    topics_count = int(topics_row["n"]) if topics_row else 0

    telegram_configured = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))

    models_ready, models_downloading = await _probe_ollama()

    return SetupStatus(
        setup_completed=setup_completed,
        models_ready=models_ready,
        models_downloading=models_downloading,
        topics_count=topics_count,
        telegram_configured=telegram_configured,
        telegram_paired=telegram_paired,
    )
