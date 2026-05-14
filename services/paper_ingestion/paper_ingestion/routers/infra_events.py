"""Internal-only endpoint for the Vector sidecar to POST infra log events.

Vector 0.40.0-alpine ships an `http` sink but no native `postgres` sink.
This endpoint accepts batches of `{level, category, source, message, context}`
and bulk-inserts them into `system_events` with `category='infra'`.

Auth uses a separate ``INFRA_INGEST_KEY`` (loaded from
``/run/secrets/infra_ingest_key``) so the orchestrator's main API key
isn't shared with infrastructure tooling.
"""

from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/infra-events", tags=["infra"])


class InfraEvent(BaseModel):
    level: str = Field(default="info")
    category: str = Field(default="infra")
    source: str
    message: str
    context: dict | None = None


def _load_ingest_key() -> str | None:
    """Load INFRA_INGEST_KEY from env or _FILE convention. Cached on first call."""
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    _cfg = get_paper_ingestion_settings()
    if _cfg.infra_ingest_key:
        return _cfg.infra_ingest_key.get_secret_value().strip() or None
    file_path = _cfg.infra_ingest_key_file
    if file_path and Path(file_path).is_file():
        try:
            return Path(file_path).read_text().strip() or None
        except OSError:
            return None
    return None


def _check_auth(provided: str | None) -> None:
    expected = _load_ingest_key()
    if not expected:
        # No key configured: refuse all writes. The Vector sidecar should
        # not be running without a key.
        raise HTTPException(status_code=503, detail="infra ingest disabled")
    if not hmac.compare_digest((provided or "").encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="invalid infra ingest key")


@router.post("")
async def ingest_infra_events(
    request: Request,
    x_infra_key: str | None = Header(default=None),
) -> dict[str, int]:
    """Bulk-insert events into system_events with category='infra'.

    Accepts either a JSON array (``application/json``) or NDJSON
    (``application/x-ndjson``, one event per line) — Vector's ``http`` sink
    with ``encoding.codec = "json"`` and ``framing.method = "newline_delimited"``
    produces NDJSON. Returns ``{"accepted": N}``.
    """
    _check_auth(x_infra_key)

    body = await request.body()
    if not body.strip():
        return {"accepted": 0}

    parsed: list[dict] = []
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                parsed = [x for x in arr if isinstance(x, dict)]
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON array") from None
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                parsed.append(obj)

    if not parsed:
        return {"accepted": 0}

    events = [InfraEvent(**raw) for raw in parsed]
    pool = request.app.state.db_pool
    rows = [
        (
            e.level if e.level in {"debug", "info", "warning", "error", "critical"} else "info",
            "infra",
            e.source[:200],
            e.message[:65535],
            e.context or {},
            None,  # correlation_id — infra events don't carry one
        )
        for e in events
    ]
    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO system_events "
                "(level, category, source, message, context, correlation_id) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
                rows,
            )
    except Exception:
        logger.exception("infra-events bulk insert failed")
        raise HTTPException(status_code=500, detail="ingest failed") from None
    return {"accepted": len(rows)}
