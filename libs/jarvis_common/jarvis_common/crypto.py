"""Fernet-based envelope encryption for JARVIS config secrets."""

from __future__ import annotations

import functools
import os

from cryptography.fernet import Fernet


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
]
