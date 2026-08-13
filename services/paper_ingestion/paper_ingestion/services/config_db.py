"""DB helpers: fetch/write config rows, resolve display value, and startup secret migration."""

import logging
from typing import Any

import asyncpg
from cryptography.fernet import InvalidToken
from jarvis_common.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)

from paper_ingestion.services.config_metadata import (
    _ENCRYPTED_KEYS,
    _SECRET_KEYS,
    _classify_config_key,
)

__all__ = [
    "_fetch_effective_config_row",
    "_write_config_row",
    "_resolve_config_value",
    "migrate_plaintext_secrets",
]

logger = logging.getLogger(__name__)

# Stands in for the masked preview when a stored secret cannot be read back.
# It is displayed to the administrator, so it names the remedy rather than the
# cause, and it is deliberately not ``None``: an absent row already means "never
# configured", and reusing it would hide a broken credential as a missing one.
_UNREADABLE_SECRET = "Unreadable — replace this value"


async def _fetch_effective_config_row(
    conn: Any,
    key: str,
    user_id: int | None,
    *,
    is_admin: bool = False,
) -> asyncpg.Record | None:
    """Return caller-specific personal config, with NULL-row fallback only for admins.

    For personal keys the NULL-row (system default) is only returned when the
    caller is an admin; regular authenticated users see only their own row
    (404 if absent) to prevent system-default leakage.
    System/unknown keys always use the NULL-row path regardless of role.
    """
    scope = _classify_config_key(key)
    if scope == "personal" and user_id is not None:
        if is_admin:
            # Admins may see system default as fallback.
            return await conn.fetchrow(
                """SELECT key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE key = $1 AND (user_id = $2 OR user_id IS NULL)
                   ORDER BY user_id IS NULL
                   LIMIT 1""",
                key,
                user_id,
            )
        # Non-admin: only return the caller's own row — no NULL-row fallback.
        return await conn.fetchrow(
            """SELECT key, value, encrypted_value, user_id
               FROM user_config
               WHERE key = $1 AND user_id = $2""",
            key,
            user_id,
        )
    return await conn.fetchrow(
        """SELECT key, value, encrypted_value, user_id
           FROM user_config
           WHERE key = $1 AND user_id IS NULL""",
        key,
    )


async def _write_config_row(
    conn: Any,
    *,
    user_id: int | None,
    key: str,
    value: Any,
    encrypted_value: bytes | None = None,
) -> None:
    if encrypted_value is not None:
        await conn.execute(
            """INSERT INTO user_config (user_id, key, value, encrypted_value)
               VALUES ($1, $2, NULL, $3)
               ON CONFLICT (user_id, key) DO UPDATE
                   SET value = NULL, encrypted_value = $3, updated_at = NOW()""",
            user_id,
            key,
            encrypted_value,
        )
        return
    await conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES ($1, $2, $3::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE
               SET value = $3::jsonb, encrypted_value = NULL, updated_at = NOW()""",
        user_id,
        key,
        value,
    )


async def _upsert_system_num_ctx(conn: Any, role: str, value: int) -> None:
    """Write the system ``llm.{role}_num_ctx`` row that prompt budgets read.

    The effective-context readers (``jarvis_common.effective_num_ctx``) resolve
    every prompt input budget against this system-scoped row. LiteLLM
    deployments are deployment-global, so the row must follow the value actually
    delivered to the proxy regardless of which machine wrote it — kept in lock-
    step here, on every delivery that attached a num_ctx, not only on the
    Settings PUT path. ``role`` is ``"smart"`` or ``"fast"`` (the LiteLLM alias).
    """
    await _write_config_row(conn, user_id=None, key=f"llm.{role}_num_ctx", value=value)


def _resolve_config_value(key: str, row: Any) -> Any:
    """Return the display value for a config row, applying masking / decryption."""
    if key in _ENCRYPTED_KEYS:
        enc = row.get("encrypted_value")
        if enc is not None:
            try:
                # Decrypt then mask — never expose plaintext over the API
                plaintext = decrypt_secret(enc.decode("ascii"))
            except (InvalidToken, UnicodeDecodeError):
                # One stored value the current key cannot read, which a restore
                # can leave behind. It must not take down the configuration
                # listing the administrator needs in order to replace it. A
                # missing or malformed configuration key is a different, global
                # failure and is deliberately still raised. Never fall through
                # to masking the stored bytes — that would present ciphertext
                # as a working value.
                logger.warning("Stored secret for %s could not be decrypted", key, exc_info=True)
                return _UNREADABLE_SECRET
            return mask_secret(plaintext)
        raw = row.get("value")
        if raw is not None:
            # Legacy plaintext row: mask without decrypting
            return mask_secret(str(raw))
        return None
    if key in _SECRET_KEYS:
        raw = row.get("value")
        return "****" if raw is not None else None
    return row.get("value")


async def migrate_plaintext_secrets(db_pool: asyncpg.Pool) -> int:
    """Eagerly re-encrypt any plaintext rows for keys in :data:`_ENCRYPTED_KEYS`.

    Older rows may still hold a plaintext secret in ``user_config.value`` while
    ``encrypted_value`` is NULL — the result of upgrading from a release that
    predated envelope encryption. This helper runs once at service startup and
    rewrites such rows in place: encrypts ``value`` into ``encrypted_value``
    and clears ``value`` so the API never returns plaintext.

    Skips rows that already have ``encrypted_value`` populated (idempotent).
    Returns the number of rows rewritten.
    """
    if not _ENCRYPTED_KEYS:
        return 0
    keys = sorted(_ENCRYPTED_KEYS)
    rewritten = 0
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, key, value FROM user_config "
            "WHERE key = ANY($1::text[]) AND value IS NOT NULL AND encrypted_value IS NULL",
            keys,
        )
        for row in rows:
            value = row["value"]
            # asyncpg JSONB codec auto-decodes — accept str or numeric values.
            if value is None:
                continue
            plaintext = value if isinstance(value, str) else str(value)
            if not plaintext:
                continue
            try:
                ciphertext_bytes = encrypt_secret(plaintext).encode("ascii")
            except Exception:
                logger.warning(
                    "migrate_plaintext_secrets: encrypt failed for key=%s; skipping",
                    row["key"],
                    exc_info=True,
                )
                continue
            await conn.execute(
                "UPDATE user_config SET value = NULL, encrypted_value = $2, updated_at = NOW() "
                "WHERE id = $1",
                row["id"],
                ciphertext_bytes,
            )
            rewritten += 1
    if rewritten:
        logger.info("migrate_plaintext_secrets: re-encrypted %d row(s)", rewritten)
    return rewritten
