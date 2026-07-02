"""Zotero per-user encrypted config loading and user-id resolution."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger("paper_ingestion.integrations.zotero_service")


class ZoteroConfigDecryptError(Exception):
    """Raised when stored Zotero config cannot be Fernet-decrypted."""


# Keys whose decrypt failure should abort config loading entirely.
# Non-listed encrypted keys are logged and skipped so callers still receive
# the partial config (e.g. last_library_version read failures are non-fatal).
_CRITICAL_ZOTERO_CONFIG_KEYS: frozenset[str] = frozenset({"api_key"})


async def _get_zotero_config(
    db_pool: asyncpg.Pool,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Read Zotero settings from user_config. Returns dict with short keys.

    Prefers encrypted_value (post-Sprint-1 UI saves) over plaintext value
    (legacy rows written before encryption was introduced).

    If decryption fails (e.g. key rotation, corrupted ciphertext) the whole
    config is treated as missing — callers will hit the "no api_key" branch
    and skip the operation gracefully.
    """
    from jarvis_common.crypto import resolve_secret_row

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
               FROM user_config
               WHERE key LIKE 'zotero.%' AND (user_id = $1 OR user_id IS NULL)
               ORDER BY key, user_id IS NULL""",
            user_id,
        )
    config: dict[str, Any] = {}
    failed_critical: list[str] = []
    failed_non_critical: list[str] = []
    for row in rows:
        short_key = row["key"][len("zotero.") :]
        enc = row.get("encrypted_value")
        if enc is not None:
            # Post-Sprint-1 row: decrypt Fernet ciphertext stored as BYTEA.
            # resolve_secret_row handles memoryview/bytes/str BYTEA variants.
            try:
                config[short_key] = resolve_secret_row({"encrypted_value": enc, "value": None})
            except Exception:
                if short_key in _CRITICAL_ZOTERO_CONFIG_KEYS:
                    logger.warning(
                        "Zotero config decrypt failed for critical key %r; "
                        "operator must re-save Zotero API key in Settings",
                        short_key,
                        exc_info=True,
                    )
                    failed_critical.append(short_key)
                else:
                    logger.warning(
                        "Zotero config decrypt failed for non-critical key %r; skipping",
                        short_key,
                        exc_info=True,
                    )
                    failed_non_critical.append(short_key)
        else:
            # Legacy plaintext row (or non-secret scalar).
            # asyncpg JSONB codec auto-decodes objects/arrays/booleans;
            # scalar strings come back as str — no manual json.loads() needed.
            config[short_key] = row["value"]
    if failed_critical:
        raise ZoteroConfigDecryptError(failed_critical[0])
    return config


async def _resolve_zotero_user_id(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    user_id: int | None,
) -> int | None:
    """Resolve the per-user owner of a ``paper_user_zotero_links`` row.

    The link table needs a concrete ``user_id``, but single-user deployments
    store Zotero config under ``user_config.user_id IS NULL`` and pass
    ``owner_user_id=None`` / ``polling_user_id=None`` through the job boundary.
    Map None -> the sole active user (mirrors migration 0101's sole-user backfill
    arm). Return None when ownership is genuinely ambiguous (None AND more than
    one active user) so callers fail safe — treat it exactly as "not linked" /
    skip the push — rather than misattribute one user's Zotero keys to another.
    """
    if user_id is not None:
        return user_id
    rows = await conn.fetch("SELECT id FROM users WHERE deleted_at IS NULL")
    return rows[0]["id"] if len(rows) == 1 else None
