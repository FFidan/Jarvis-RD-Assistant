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
import ipaddress
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from jarvis_common.auth import _client_ip, _raw_socket_ip
from jarvis_common.secrets_files import read_secret_with_file_fallback
from jarvis_common.settings import get_core_settings
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/infra-events", tags=["infra"])

_INFRA_CACHED_ALLOWED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None

# Cap the number of events accepted per request. The Vector sidecar
# retries on any non-2xx, so a hard 413 on an oversized batch would trigger an
# infinite retry storm. Instead we accept up to this many events, count the
# overflow as ``skipped``, and still return 200 — Vector sees a successful
# delivery and does not retry. Vector's own batching keeps normal batches well
# under this bound; only a misconfigured/abusive client hits the cap.
_MAX_INFRA_BATCH = 1000

# Reject once the streamed body exceeds this bound, preventing unbounded memory
# growth from a single request regardless of transfer-encoding. 10 MB is
# generous: 1000 events × ~1 KB each is ~1 MB; this gives 10× headroom while
# still rejecting obviously abusive payloads.
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB


def _parse_infra_allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = get_core_settings().infra_ingest_allowed_cidrs
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("INFRA_INGEST_ALLOWED_CIDRS: invalid CIDR %r — skipping", part)
    return networks


def _infra_ip_in_allowlist(ip_str: str | None) -> bool:
    global _INFRA_CACHED_ALLOWED_NETWORKS
    if not ip_str:
        return False
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        logger.warning("infra-ingest: could not parse client IP %r as address — denying", ip_str)
        return False
    if _INFRA_CACHED_ALLOWED_NETWORKS is None:
        _INFRA_CACHED_ALLOWED_NETWORKS = _parse_infra_allowed_networks()
    for net in _INFRA_CACHED_ALLOWED_NETWORKS:
        if addr in net:
            return True
    return False


class InfraEvent(BaseModel):
    level: str = Field(default="info")
    category: str = Field(default="infra")
    source: str
    message: str
    context: dict | None = None


def _load_ingest_key() -> str | None:
    """Load INFRA_INGEST_KEY from the env value or the ``_FILE`` convention."""
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    _cfg = get_paper_ingestion_settings()
    if _cfg.infra_ingest_key:
        return _cfg.infra_ingest_key.get_secret_value().strip() or None
    file_path = _cfg.infra_ingest_key_file
    if file_path and Path(file_path).is_file():
        return read_secret_with_file_fallback(None, file_path)
    return None


def _check_auth(request: Request, provided: str | None) -> None:
    # Default-deny: empty CIDR config means infra ingest is not provisioned.
    # Distinct from "key not configured" (503 below): operator must explicitly
    # opt-in to the source-IP surface by setting INFRA_INGEST_ALLOWED_CIDRS.
    if not get_core_settings().infra_ingest_allowed_cidrs.strip():
        raise HTTPException(status_code=503, detail="INFRA_INGEST_ALLOWED_CIDRS not configured")
    client_ip = _client_ip(request)
    raw_ip, raw_stashed = _raw_socket_ip(request)
    if not raw_stashed:
        # App built without RawClientStashMiddleware (e.g. a bare test app, which
        # also installs no ProxyHeadersMiddleware) → request.client IS the real
        # peer; fall back to the single check on it.
        raw_ip = client_ip
    if not (_infra_ip_in_allowlist(client_ip) and _infra_ip_in_allowlist(raw_ip)):
        logger.warning(
            "infra-ingest: rejected request — client IP %s / raw socket peer %s not in allowlist",
            client_ip,
            raw_ip,
        )
        raise HTTPException(status_code=403, detail="source IP not in infra ingest allowlist")
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
    produces NDJSON. Returns ``{"accepted": N, "skipped": M}``.
    """
    _check_auth(request, x_infra_key)

    # Bound the body by bytes actually read so a chunked transfer (no
    # Content-Length header) cannot bypass the cap and grow memory unbounded.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")

    if not body.strip():
        return {"accepted": 0, "skipped": 0}

    parsed: list[dict] = []
    skipped = 0
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
                skipped += 1
                continue
            if isinstance(obj, dict):
                parsed.append(obj)

    if skipped:
        logger.warning("infra-events: skipped %d malformed NDJSON lines", skipped)

    # Cap the batch. Count the overflow as skipped and return 200 so
    # Vector (which retries on any non-2xx) does not enter a retry storm.
    if len(parsed) > _MAX_INFRA_BATCH:
        overflow = len(parsed) - _MAX_INFRA_BATCH
        skipped += overflow
        parsed = parsed[:_MAX_INFRA_BATCH]
        logger.warning(
            "infra-events: batch exceeded cap (%d); dropped %d events",
            _MAX_INFRA_BATCH,
            overflow,
        )

    if not parsed:
        return {"accepted": 0, "skipped": skipped}

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
    return {"accepted": len(rows), "skipped": skipped}
