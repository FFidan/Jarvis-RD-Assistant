"""Fernet-based envelope encryption for JARVIS config secrets."""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, MultiFernet

from jarvis_common.settings import get_core_settings

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _load_fernet() -> Fernet | MultiFernet:
    """Build a Fernet (or MultiFernet) instance from env vars.

    Reads ``JARVIS_CONFIG_KEY`` (the *current* / write key) plus the optional
    ``JARVIS_CONFIG_KEY_OLD`` (a previous key kept for read-only decryption
    during a rotation window). Both honour the Docker Secret ``_FILE``
    convention via :class:`jarvis_common.settings.SecretsSettings`. When ``OLD`` is
    set, returns a :class:`MultiFernet([new, old])` so:

    * ``encrypt`` always uses the new key (first in the list),
    * ``decrypt`` accepts ciphertexts produced under either key.

    This enables zero-downtime key rotation: deploy with both vars set, run a
    background re-encrypt, then drop ``OLD`` on the next deploy.

    Cached for process lifetime so repeated calls don't re-parse the keys.
    Send SIGHUP after rotating ``JARVIS_CONFIG_KEY`` to clear the cache (see
    :func:`reload_fernet_on_sighup`).
    Raises RuntimeError if the new key is unset or malformed.
    """
    from jarvis_common.settings import SecretsSettings  # noqa: PLC0415

    # Use a fresh snapshot so refresh_fernet_cache() + monkeypatch works in tests.
    s = SecretsSettings()
    raw_new = s.jarvis_config_key.get_secret_value() if s.jarvis_config_key else ""
    if not raw_new:
        raise RuntimeError(
            "JARVIS_CONFIG_KEY is not set. "
            "Generate one with: python -c "
            "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    try:
        new_fernet = Fernet(raw_new.encode() if isinstance(raw_new, str) else raw_new)
    except Exception as exc:
        raise RuntimeError(
            f"JARVIS_CONFIG_KEY is malformed (expected urlsafe base64-encoded 32-byte key): {exc}"
        ) from exc

    raw_old = s.jarvis_config_key_old.get_secret_value() if s.jarvis_config_key_old else ""
    if not raw_old:
        return new_fernet
    try:
        old_fernet = Fernet(raw_old.encode() if isinstance(raw_old, str) else raw_old)
    except Exception as exc:
        raise RuntimeError(
            "JARVIS_CONFIG_KEY_OLD is malformed (expected urlsafe base64-encoded "
            f"32-byte key): {exc}"
        ) from exc
    # MultiFernet([new, old]) — encrypts with new, decrypts with either.
    return MultiFernet([new_fernet, old_fernet])


def refresh_fernet_cache() -> None:
    """Clear the cached Fernet instance so the next call re-reads JARVIS_CONFIG_KEY.

    Tests that monkeypatch JARVIS_CONFIG_KEY after import must call this so the
    cached instance reflects the new environment.
    """
    _load_fernet.cache_clear()


def reload_fernet_on_sighup() -> None:
    """Register SIGHUP → clear _load_fernet cache.

    Operators send SIGHUP after rotating CONFIG_ENC_KEY so the next
    call to :func:`encrypt_secret` or :func:`decrypt_secret` re-reads
    the new key from the environment without a full process restart.

    Safe to call from any service startup path; the handler is a no-op if
    SIGHUP is not available (e.g. Windows) or the caller is not the main thread.
    """
    import signal

    try:
        signal.signal(signal.SIGHUP, lambda *_: _load_fernet.cache_clear())
    except (ValueError, OSError):
        # Not in main thread, or platform without SIGHUP (Windows).
        pass


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


def resolve_secret_row(
    row: asyncpg.Record | Mapping[str, Any] | None,
) -> str | None:
    """Return the plaintext value for a ``user_config``-shaped row.

    Reads ``encrypted_value`` first (Fernet ciphertext stored as BYTEA,
    accepting memoryview / bytes / str), falling back to the legacy
    plaintext ``value`` column. Returns ``None`` when both are absent
    or NULL. Raises whatever ``decrypt_secret`` raises when an encrypted
    row cannot be decrypted — callers that want graceful degradation
    should wrap the call in ``try/except``.

    Accepts either an ``asyncpg.Record`` (which supports ``dict()``
    coercion) or any ``Mapping[str, Any]`` (e.g. a plain ``dict``).
    """
    if row is None:
        return None
    # asyncpg.Record is not a virtual subclass of Mapping; normalise both
    # forms into a Mapping so we get static type guarantees downstream.
    data: Mapping[str, Any] = row if isinstance(row, Mapping) else dict(row)
    enc = data.get("encrypted_value")
    raw = data.get("value")
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
        dev_mode = get_core_settings().dev_mode

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
            logger.warning(
                "%s Continuing because DEV_MODE=true. Cause: %s",
                message,
                exc,
                exc_info=True,
            )
            return len(rows)
        raise RuntimeError(message) from exc

    return len(rows)


def mask_secret(plaintext: str) -> str:
    """Return a masked preview of *plaintext*.

    Shows only the trailing 4 characters — never the prefix — so the masked
    form leaks no information about how the secret was generated (e.g. the
    ``sk-ant-`` provider prefix on Anthropic API keys).

    - Empty string -> ''
    - Length < 4    -> '****'
    - Otherwise     -> '****' + last 4 chars
    """
    if not plaintext:
        return ""
    if len(plaintext) < 4:
        return "****"
    return "****" + plaintext[-4:]


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
    "reload_fernet_on_sighup",
    "resolve_secret_row",
    "validate_encrypted_config_rows",
]
