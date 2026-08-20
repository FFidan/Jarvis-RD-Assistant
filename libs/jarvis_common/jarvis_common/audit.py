"""Audit log helper: append security and destructive-mutation events."""

import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Any

import asyncpg

from jarvis_common.settings import get_secrets_settings

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
# Resource strings are delimited paths ("users/12", "telegram:pairing:user:12").
# Splitting on the delimiters keeps the separators in the result, so joining the
# parts back together reproduces the input except for the segments replaced.
_RESOURCE_DELIMITER_RE = re.compile(r"([/:._-])")
# Metadata keys that carry a caller's network address.
_SOURCE_ADDRESS_KEYS = ("ip", "client_ip", "raw_client_ip")
# 48 bits of SHA-256: wide enough to tell sources apart in an audit trail, and
# small enough to survive JSON transport to the audit view, which reads numbers
# as doubles and would silently round anything above 2**53.
_ADDRESS_DIGEST_HEX_CHARS = 12
#: Domain separation, so this digest cannot be compared against any other
#: value derived from the same install secret.
_ADDRESS_DIGEST_LABEL = b"jarvis-audit-address-v1"
#: Used only when no install secret is configured, which production start-up
#: refuses. Random per process: a run stays correlatable and no digest is
#: reversible, at the cost of correlation across a restart.
_EPHEMERAL_ADDRESS_KEY = secrets.token_bytes(32)


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


def _address_digest(address: str) -> int:
    """Return a stable pseudonym for a caller's network *address*.

    Keyed rather than hashed, because the space this pseudonymises is small
    enough to enumerate: every IPv4 address is one of about four billion, so
    an unkeyed digest of one can simply be looked up. A keyed digest cannot,
    which is what lets an immutable row carry the value at all.
    """
    key = get_secrets_settings().jarvis_model_hmac_key
    keyed = hmac.new(
        key.get_secret_value().encode("utf-8") if key else _EPHEMERAL_ADDRESS_KEY,
        _ADDRESS_DIGEST_LABEL + address.encode("utf-8"),
        hashlib.sha256,
    )
    return int(keyed.hexdigest()[:_ADDRESS_DIGEST_HEX_CHARS], 16)


def _immutable_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return audit-safe facts without erasable text values.

    The mutable Platform subject mapping owns identity and personal metadata.
    Immutable audit rows retain only typed operational facts, never arbitrary
    text that could contain an erasable identifier.

    A caller's network address is the one exception, and it is kept as a
    digest rather than dropped: an authentication failure with no attributable
    source cannot be correlated at all, so repeated attempts from one address
    would be indistinguishable from unrelated ones.
    """
    capped = _cap_metadata(metadata)
    immutable = {
        key: value
        for key, value in capped.items()
        if key not in _ERASABLE_METADATA_KEYS and isinstance(value, bool | int | float | type(None))
    }
    for key in _SOURCE_ADDRESS_KEYS:
        address = capped.get(key)
        if isinstance(address, str) and address:
            immutable["ip_hash"] = _address_digest(address)
            break
    return immutable


def _immutable_resource(resource: str, user_id: str | None) -> str:
    """Return a bounded non-identifying resource accepted by the database.

    Only a whole delimited segment equal to *user_id* is pseudonymised. A
    substring replacement would rename unrelated objects — for actor ``1``,
    ``milestone:137`` became ``milestone:subject37`` and ``paper:1001`` became
    ``paper:subject00subject`` — so distinct actions collapsed onto identical
    strings in a table that cannot be corrected afterwards.
    """
    if user_id:
        parts = _RESOURCE_DELIMITER_RE.split(resource)
        sanitized = "".join("subject" if part == user_id else part for part in parts)
    else:
        sanitized = resource
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
