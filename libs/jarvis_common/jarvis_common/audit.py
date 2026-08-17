"""Audit log helper: append security and destructive-mutation events."""

import hashlib
import json
import logging
import re
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# Hard ceiling on serialised JSONB metadata size (bytes). Audit events are
# best-effort writes; oversized payloads (e.g. an attacker-controlled blob in
# a request body that found its way into metadata) would bloat audit_log
# without value. Above this threshold we replace the payload with a marker.
_METADATA_MAX_BYTES = 4096
_ERASABLE_METADATA_KEYS = frozenset(
    {"email", "ip", "client_ip", "raw_client_ip", "name", "username", "user_agent", "user_id"}
)
_SAFE_RESOURCE_RE = re.compile(r"^/?[a-z][a-z0-9_./:-]{0,255}$")


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


def _immutable_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return audit-safe facts without erasable text values.

    The mutable Platform subject mapping owns identity and personal metadata.
    Immutable audit rows retain only typed operational facts, never arbitrary
    text that could contain an erasable identifier.
    """
    capped = _cap_metadata(metadata)
    return {
        key: value
        for key, value in capped.items()
        if key not in _ERASABLE_METADATA_KEYS and isinstance(value, bool | int | float | type(None))
    }


def _immutable_resource(resource: str, user_id: str | None) -> str:
    """Return a bounded non-identifying resource accepted by the database."""
    sanitized = resource.replace(user_id, "subject") if user_id else resource
    if _SAFE_RESOURCE_RE.fullmatch(sanitized):
        return sanitized
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
    return f"resource_hash:{digest}"


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
        SELECT platform.append_audit_event($1, $2, $3, $4::jsonb)
        """,
        user_id,
        action,
        _immutable_resource(resource, user_id),
        _immutable_metadata(metadata),
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
    immutable_metadata = _immutable_metadata(metadata)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    SELECT platform.append_audit_event($1, $2, $3, $4::jsonb)
                    """,
                    user_id,
                    action,
                    _immutable_resource(resource, user_id),
                    immutable_metadata,
                )
    except Exception as exc:
        logger.warning("audit_log insert failed: %r", exc, exc_info=True)
