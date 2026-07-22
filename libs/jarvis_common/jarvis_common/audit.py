"""Audit log helper: append security and destructive-mutation events."""

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# Hard ceiling on serialised JSONB metadata size (bytes). Audit events are
# best-effort writes; oversized payloads (e.g. an attacker-controlled blob in
# a request body that found its way into metadata) would bloat audit_log
# without value. Above this threshold we replace the payload with a marker.
_METADATA_MAX_BYTES = 4096


def _cap_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-native *metadata* dict, or a truncation marker if too large.

    JSON-encodes the payload to measure its serialised size (since asyncpg's
    JSONB codec stores JSON bytes). If the encoded form exceeds
    :data:`_METADATA_MAX_BYTES`, returns
    ``{"_truncated": True, "_size": <orig bytes>}`` so the audit row is still
    written and the size is recoverable, just without the runaway payload.

    Defensive against ``TypeError`` (non-serialisable values) — falls back to
    ``str()`` so audit logging stays best-effort. The database's JSONB codec
    is registered with plain ``json.dumps`` (no ``default=str``), so a
    non-native value (``datetime``, ``UUID``, ...) that measured under the
    cap must not be handed back as-is: it would still fail the codec's own
    encode and silently drop the row. Decoding the already-sanitised
    ``encoded`` string back to a dict guarantees the returned value is what
    was measured and is codec-compatible by construction.
    """
    if not metadata:
        return {}
    try:
        encoded = json.dumps(metadata, default=str, ensure_ascii=False)
    except TypeError:
        encoded = json.dumps({k: str(v) for k, v in metadata.items()}, ensure_ascii=False)
    size = len(encoded.encode("utf-8"))
    if size > _METADATA_MAX_BYTES:
        return {"_truncated": True, "_size": size}
    return json.loads(encoded)


async def log_audit_strict(
    conn: Any,
    *,
    action: str,
    resource: str,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert an audit event on the caller's transaction and propagate failure."""
    await conn.execute(
        """
        INSERT INTO audit_log (user_id, action, resource, metadata)
        VALUES ($1, $2, $3, $4)
        """,
        user_id,
        action,
        resource,
        _cap_metadata(metadata),
    )


async def log_audit(
    pool: asyncpg.Pool,
    *,
    action: str,
    resource: str,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert an audit event. Never raises — best-effort logging.

    ``metadata`` is JSON-encoded and capped at 4 KB by :func:`_cap_metadata`
    to prevent a single oversize event from bloating the audit_log table.
    """
    capped = _cap_metadata(metadata)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (user_id, action, resource, metadata)
                VALUES ($1, $2, $3, $4)
                """,
                user_id,
                action,
                resource,
                capped,
            )
    except Exception as exc:
        logger.warning("audit_log insert failed: %r", exc, exc_info=True)
