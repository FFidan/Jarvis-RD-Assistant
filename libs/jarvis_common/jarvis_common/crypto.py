"""Fernet-based envelope encryption for JARVIS config secrets."""

from __future__ import annotations

import functools
import logging
import os
from typing import Any

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _load_fernet() -> Fernet:
    """Build a Fernet instance from JARVIS_CONFIG_KEY env var.

    Cached so repeated calls don't re-parse the key.
    Raises RuntimeError if the key is unset or malformed.
    """
    raw = os.environ.get("JARVIS_CONFIG_KEY", "")
    if not raw:
        raise RuntimeError(
            "JARVIS_CONFIG_KEY is not set. "
            "Generate one with: python -c "
            "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception as exc:
        raise RuntimeError(
            f"JARVIS_CONFIG_KEY is malformed (expected urlsafe base64-encoded 32-byte key): {exc}"
        ) from exc


def refresh_fernet_cache() -> None:
    """Clear the cached Fernet instance so the next call re-reads JARVIS_CONFIG_KEY.

    Tests that monkeypatch JARVIS_CONFIG_KEY after import must call this so the
    cached instance reflects the new environment.
    """
    _load_fernet.cache_clear()


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* with the key from JARVIS_CONFIG_KEY.

    Returns the ciphertext as a urlsafe base64 string.
    Raises RuntimeError if JARVIS_CONFIG_KEY is unset or malformed.
    """
    fernet = _load_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a ciphertext produced by encrypt_secret.

    Raises RuntimeError if JARVIS_CONFIG_KEY is unset or malformed.
    Raises cryptography.fernet.InvalidToken on tamper or wrong key.
    """
    fernet = _load_fernet()
    return fernet.decrypt(ciphertext.encode()).decode()


def resolve_secret_row(row: Any) -> str | None:
    """Return the plaintext value for a ``user_config``-shaped row.

    Reads ``encrypted_value`` first (Fernet ciphertext stored as BYTEA,
    accepting memoryview / bytes / str), falling back to the legacy
    plaintext ``value`` column. Returns ``None`` when both are absent
    or NULL. Raises whatever ``decrypt_secret`` raises when an encrypted
    row cannot be decrypted — callers that want graceful degradation
    should wrap the call in ``try/except``.
    """
    if row is None:
        return None
    get = getattr(row, "get", None)
    if callable(get):
        enc = row.get("encrypted_value")
        raw = row.get("value")
    else:
        enc = row["encrypted_value"] if "encrypted_value" in row else None
        raw = row["value"] if "value" in row else None
    if enc is not None:
        if isinstance(enc, memoryview):
            enc = enc.tobytes()
        if isinstance(enc, bytes | bytearray):
            ciphertext = bytes(enc).decode("ascii")
        else:
            ciphertext = str(enc)
        return decrypt_secret(ciphertext)
    if raw is None:
        return None
    return str(raw)


async def validate_encrypted_config_rows(
    db_pool: Any,
    *,
    dev_mode: bool | None = None,
) -> int:
    """Validate that encrypted ``user_config`` rows are decryptable.

    In non-dev mode, startup fails when encrypted rows exist but
    ``JARVIS_CONFIG_KEY`` is missing, malformed, or unable to decrypt them.
    In dev mode, the same condition is logged and startup continues.

    Returns
    -------
    int
        Number of encrypted rows checked.
    """
    if dev_mode is None:
        dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, encrypted_value
            FROM user_config
            WHERE encrypted_value IS NOT NULL
            ORDER BY key
            """
        )

    if not rows:
        return 0

    try:
        for row in rows:
            encrypted_value = row["encrypted_value"]
            if isinstance(encrypted_value, memoryview):
                encrypted_value = encrypted_value.tobytes()
            if isinstance(encrypted_value, bytes | bytearray):
                ciphertext = bytes(encrypted_value).decode("ascii")
            else:
                ciphertext = str(encrypted_value)
            decrypt_secret(ciphertext)
    except Exception as exc:
        message = (
            "Encrypted user_config rows exist, but JARVIS_CONFIG_KEY cannot decrypt them. "
            "Set the original Fernet key or rotate the key before starting services."
        )
        if dev_mode:
            logger.warning("%s Continuing because DEV_MODE=true. Cause: %s", message, exc)
            return len(rows)
        raise RuntimeError(message) from exc

    return len(rows)


def mask_secret(plaintext: str) -> str:
    """Return a masked preview of *plaintext*.

    - Empty string -> ''
    - Length <= 4   -> '****'
    - Otherwise     -> first 4 chars + '****'
    """
    if not plaintext:
        return ""
    if len(plaintext) <= 4:
        return "****"
    return plaintext[:4] + "****"


def rotate_key(old_key: bytes, new_key: bytes, ciphertext: str) -> str:
    """Decrypt *ciphertext* with *old_key* and re-encrypt with *new_key*.

    Returns the new ciphertext string.
    Raises cryptography.fernet.InvalidToken if old_key cannot decrypt the token.
    """
    plaintext = Fernet(old_key).decrypt(ciphertext.encode()).decode()
    return Fernet(new_key).encrypt(plaintext.encode()).decode()


__all__ = [
    "encrypt_secret",
    "decrypt_secret",
    "mask_secret",
    "rotate_key",
    "refresh_fernet_cache",
    "validate_encrypted_config_rows",
]
