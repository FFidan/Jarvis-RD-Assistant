"""Tests for jarvis_common.crypto envelope-encryption module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet, InvalidToken
from jarvis_common.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    refresh_fernet_cache,
    rotate_key,
    validate_encrypted_config_rows,
)


class FakeRecord(dict):
    """Small asyncpg.Record stand-in for crypto tests."""


def _make_pool(rows: list[FakeRecord]):
    conn = AsyncMock()
    conn.fetch.return_value = rows
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def valid_key(monkeypatch) -> bytes:
    """Generate a fresh Fernet key and wire it into JARVIS_CONFIG_KEY."""
    key = Fernet.generate_key()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key.decode())
    refresh_fernet_cache()
    return key


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("valid_key")
def test_roundtrip() -> None:
    """encrypt_secret then decrypt_secret returns the original plaintext."""
    plaintext = "super-secret-api-key-12345"
    ciphertext = encrypt_secret(plaintext)
    assert decrypt_secret(ciphertext) == plaintext


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("valid_key")
def test_tamper_raises() -> None:
    """Modifying a character of the ciphertext causes decrypt to raise InvalidToken."""
    ciphertext = encrypt_secret("my-secret")
    # Flip the last character
    tampered = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")
    with pytest.raises(InvalidToken):
        decrypt_secret(tampered)


# ---------------------------------------------------------------------------
# Wrong key
# ---------------------------------------------------------------------------


def test_wrong_key_raises(monkeypatch) -> None:
    """Ciphertext encrypted under one key cannot be decrypted with another."""
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()

    # Encrypt with key_a
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key_a.decode())
    refresh_fernet_cache()
    ciphertext = encrypt_secret("secret-value")

    # Switch to key_b and attempt decryption
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key_b.decode())
    refresh_fernet_cache()
    with pytest.raises(InvalidToken):
        decrypt_secret(ciphertext)


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


def test_rotation_works(valid_key) -> None:
    """rotate_key decrypts with old key and re-encrypts with new key correctly."""
    old_key = valid_key
    new_key = Fernet.generate_key()

    plaintext = "rotate-me"
    # Encrypt with old key (via the live cache)
    ciphertext_old = encrypt_secret(plaintext)

    # Rotate to new key
    ciphertext_new = rotate_key(old_key, new_key, ciphertext_old)

    # New key can decrypt
    assert Fernet(new_key).decrypt(ciphertext_new.encode()).decode() == plaintext

    # Old key cannot decrypt the new ciphertext
    with pytest.raises(InvalidToken):
        Fernet(old_key).decrypt(ciphertext_new.encode())


# ---------------------------------------------------------------------------
# Missing env var
# ---------------------------------------------------------------------------


def test_missing_env_raises(monkeypatch) -> None:
    """Calling encrypt_secret without JARVIS_CONFIG_KEY raises RuntimeError."""
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    refresh_fernet_cache()
    with pytest.raises(RuntimeError, match="JARVIS_CONFIG_KEY"):
        encrypt_secret("anything")


# ---------------------------------------------------------------------------
# mask_secret
# ---------------------------------------------------------------------------


def test_mask_secret() -> None:
    """mask_secret covers empty, short (<= 4), and long inputs."""
    assert mask_secret("") == ""
    assert mask_secret("ab") == "****"
    assert mask_secret("abcd") == "****"
    assert mask_secret("abcde") == "abcd****"
    assert mask_secret("supersecret") == "supe****"


@pytest.mark.asyncio
async def test_validate_encrypted_config_rows_accepts_decryptable_rows(valid_key) -> None:
    ciphertext = Fernet(valid_key).encrypt(b"secret")
    pool, conn = _make_pool(
        [FakeRecord({"key": "llm.openai.api_key", "encrypted_value": ciphertext})]
    )

    checked = await validate_encrypted_config_rows(pool, dev_mode=False)

    assert checked == 1
    conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_encrypted_config_rows_fails_non_dev_on_missing_key(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    refresh_fernet_cache()
    pool, _conn = _make_pool([FakeRecord({"key": "zotero.api_key", "encrypted_value": b"abc"})])

    with pytest.raises(RuntimeError, match="Encrypted user_config rows exist"):
        await validate_encrypted_config_rows(pool, dev_mode=False)


@pytest.mark.asyncio
async def test_validate_encrypted_config_rows_warns_in_dev_on_bad_key(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    refresh_fernet_cache()
    pool, _conn = _make_pool([FakeRecord({"key": "zotero.api_key", "encrypted_value": b"abc"})])

    checked = await validate_encrypted_config_rows(pool, dev_mode=True)

    assert checked == 1
