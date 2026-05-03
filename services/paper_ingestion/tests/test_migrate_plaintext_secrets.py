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
    from paper_ingestion.routers.settings import migrate_plaintext_secrets

    rows = [
        {"key": "zotero.api_key", "value": "legacy-plaintext"},
        {"key": "llm.openai.api_key", "value": "sk-openai-legacy"},
    ]
    pool, conn = _make_pool_with_rows(rows)

    rewritten = await migrate_plaintext_secrets(pool)

    assert rewritten == 2
    # Two UPDATE calls — one per row
    assert conn.execute.await_count == 2
    # Each call's $2 (the ciphertext) must decrypt back to the original plaintext
    plaintext_by_key = {row["key"]: row["value"] for row in rows}
    for call in conn.execute.await_args_list:
        sql = call.args[0]
        assert "UPDATE user_config" in sql
        assert "encrypted_value = $2" in sql
        key_arg = call.args[1]
        ciphertext_bytes = call.args[2]
        # ciphertext is bytes; decode to str then run decrypt_secret
        ct = ciphertext_bytes.decode("ascii")
        assert decrypt_secret(ct) == plaintext_by_key[key_arg]


@pytest.mark.asyncio
async def test_migrate_skips_when_no_plaintext_rows(fernet_key):
    from paper_ingestion.routers.settings import migrate_plaintext_secrets

    pool, conn = _make_pool_with_rows([])
    rewritten = await migrate_plaintext_secrets(pool)
    assert rewritten == 0
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_migrate_skips_null_or_empty_values(fernet_key):
    """Rows where value is empty must not be written."""
    from paper_ingestion.routers.settings import migrate_plaintext_secrets

    rows = [
        {"key": "zotero.api_key", "value": ""},  # empty string -> skipped
    ]
    pool, conn = _make_pool_with_rows(rows)
    rewritten = await migrate_plaintext_secrets(pool)
    assert rewritten == 0
    conn.execute.assert_not_awaited()
