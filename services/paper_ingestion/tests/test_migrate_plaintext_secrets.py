"""Tests for migrate_plaintext_secrets — eager re-encryption at startup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from jarvis_common.crypto import decrypt_secret, refresh_fernet_cache


@pytest.fixture()
def fernet_key(monkeypatch):
    """Provision JARVIS_CONFIG_KEY with a fresh Fernet key for each test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key)
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    refresh_fernet_cache()
    yield key
    refresh_fernet_cache()


# Keep local: returns (pool, conn) and exposes conn.execute for assertion — extra
# semantics not covered by jarvis_common.make_pool_and_conn's fetch_return kwarg.
def _make_pool_with_rows(rows: list[dict]):
    """Build a mock asyncpg pool whose fetch() returns *rows*.

    Captures execute() calls on the connection so the test can assert what
    the migration helper rewrote.
    """
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.mark.asyncio
async def test_migrate_rewrites_plaintext_rows(fernet_key):
    from paper_ingestion.services.config_db import migrate_plaintext_secrets

    rows = [
        {"id": 1, "key": "zotero.api_key", "value": "legacy-plaintext"},
        {"id": 2, "key": "llm.openai.api_key", "value": "sk-openai-legacy"},
    ]
    pool, conn = _make_pool_with_rows(rows)

    rewritten = await migrate_plaintext_secrets(pool)

    assert rewritten == 2
    # Two UPDATE calls — one per row
    assert conn.execute.await_count == 2
    # Each call's $2 (the ciphertext) must decrypt back to the original plaintext
    plaintext_by_id = {row["id"]: row["value"] for row in rows}
    for call in conn.execute.await_args_list:
        sql = call.args[0]
        assert "UPDATE user_config" in sql
        assert "encrypted_value = $2" in sql
        row_id = call.args[1]
        ciphertext_bytes = call.args[2]
        # ciphertext is bytes; decode to str then run decrypt_secret
        ct = ciphertext_bytes.decode("ascii")
        assert decrypt_secret(ct) == plaintext_by_id[row_id]


@pytest.mark.asyncio
async def test_migrate_skips_when_no_plaintext_rows(fernet_key):
    from paper_ingestion.services.config_db import migrate_plaintext_secrets

    pool, conn = _make_pool_with_rows([])
    rewritten = await migrate_plaintext_secrets(pool)
    assert rewritten == 0
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_migrate_skips_null_or_empty_values(fernet_key):
    """Rows where value is empty must not be written."""
    from paper_ingestion.services.config_db import migrate_plaintext_secrets

    rows = [
        {"key": "zotero.api_key", "value": ""},  # empty string -> skipped
    ]
    pool, conn = _make_pool_with_rows(rows)
    rewritten = await migrate_plaintext_secrets(pool)
    assert rewritten == 0
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# CRED-01: telegram.bot_token must be treated as a secret + encrypted
# ---------------------------------------------------------------------------


def test_telegram_bot_token_in_secret_keys():
    """CRED-01: telegram.bot_token must be in _SECRET_KEYS so it is masked by list_config."""
    from paper_ingestion.services.config_metadata import _SECRET_KEYS

    assert "telegram.bot_token" in _SECRET_KEYS


def test_telegram_bot_token_in_encrypted_keys():
    """CRED-01: telegram.bot_token must be in _ENCRYPTED_KEYS so migrate_plaintext_secrets re-encrypts it."""
    from paper_ingestion.services.config_metadata import _ENCRYPTED_KEYS

    assert "telegram.bot_token" in _ENCRYPTED_KEYS


def test_telegram_bot_token_not_in_allowed_config_keys():
    """CRED-01: telegram.bot_token must NOT be writable via /api/config (out-of-band setup only)."""
    from paper_ingestion.services.config_metadata import _ALLOWED_CONFIG_KEYS

    assert "telegram.bot_token" not in _ALLOWED_CONFIG_KEYS


@pytest.mark.asyncio
async def test_migrate_rewrites_telegram_bot_token(fernet_key):
    """CRED-01: a plaintext telegram.bot_token row is re-encrypted by migrate_plaintext_secrets."""
    from paper_ingestion.services.config_db import migrate_plaintext_secrets

    raw_token = "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    rows = [{"id": 99, "key": "telegram.bot_token", "value": raw_token}]
    pool, conn = _make_pool_with_rows(rows)

    rewritten = await migrate_plaintext_secrets(pool)

    assert rewritten == 1
    assert conn.execute.await_count == 1
    call = conn.execute.await_args_list[0]
    # The ciphertext must round-trip back to the original token (behavioral proof
    # that the plaintext row was re-encrypted — no SQL-substring assertion, per TS-02).
    ciphertext_bytes = call.args[2]
    ct = ciphertext_bytes.decode("ascii")
    assert decrypt_secret(ct) == raw_token


def test_resolve_config_value_masks_telegram_bot_token():
    """CRED-01: _resolve_config_value returns masked output for a plaintext telegram.bot_token row."""
    from paper_ingestion.services.config_db import _resolve_config_value

    row = {"value": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", "encrypted_value": None}
    result = _resolve_config_value("telegram.bot_token", row)
    # Must be masked — not the raw token
    assert result is not None
    assert "1234567890" not in str(result)
