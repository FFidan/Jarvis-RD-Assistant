"""UI-1: SMTP runtime-effective config tests.

The magic-link sender must resolve SMTP from ``user_config`` (wizard-written,
Fernet-encrypted password) layered over the env (``SecretsSettings``), so a
wizard-saved relay sends mail with NO restart / hand-edited .env.

Pool is mocked asyncpg-shaped (no Docker). The real aiosmtplib send is
monkeypatched to capture the kwargs the sender would deliver with.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jarvis_common.email as email_mod
import pytest

_FERNET_KEY = "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs="


def _build_mock_pool(rows: list[dict]) -> MagicMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


@pytest.fixture()
def _crypto_key(monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_KEY", _FERNET_KEY)
    from jarvis_common.crypto import _load_fernet

    _load_fernet.cache_clear()
    yield
    _load_fernet.cache_clear()


@pytest.fixture()
def _no_env_smtp(monkeypatch):
    """Ensure SecretsSettings reports no env SMTP, so DB rows are the only source."""
    for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"):
        monkeypatch.delenv(var, raising=False)
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()
    yield
    get_secrets_settings.cache_clear()


@pytest.fixture()
def _not_dev_mode(monkeypatch):
    """Force the real-SMTP branch (not the dev log-only fallback)."""
    monkeypatch.setattr(email_mod, "_dev_mode", lambda: False)


def _capture_aiosmtplib(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def _fake_send(message, **kw):
        sent.append({"message": message, **kw})

    monkeypatch.setitem(sys.modules, "aiosmtplib", SimpleNamespace(send=_fake_send))
    return sent


@pytest.mark.asyncio
async def test_send_uses_db_smtp_with_decrypted_password(
    monkeypatch, _crypto_key, _no_env_smtp, _not_dev_mode
) -> None:
    """DB row present → sender uses DB host/port/user/from + decrypted pass."""
    from jarvis_common.crypto import encrypt_secret

    rows = [
        {"key": "smtp.host", "value": "db.relay.example", "encrypted_value": None},
        {"key": "smtp.port", "value": 2525, "encrypted_value": None},
        {"key": "smtp.user", "value": "db-user", "encrypted_value": None},
        {"key": "smtp.from", "value": "db@example.com", "encrypted_value": None},
        {
            "key": "smtp.pass",
            "value": None,
            "encrypted_value": encrypt_secret("db-secret").encode("ascii"),
        },
    ]
    pool = _build_mock_pool(rows)
    sent = _capture_aiosmtplib(monkeypatch)

    await email_mod.send_magic_link("u@example.com", "https://x/verify?token=t", pool=pool)

    assert len(sent) == 1
    call = sent[0]
    assert call["hostname"] == "db.relay.example"
    assert call["port"] == 2525
    assert call["username"] == "db-user"
    assert call["password"] == "db-secret"
    assert call["message"]["From"] == "db@example.com"


@pytest.mark.asyncio
async def test_env_fallback_when_db_empty(monkeypatch, _crypto_key, _not_dev_mode) -> None:
    """DB has no SMTP rows → sender falls back to env (SecretsSettings)."""
    monkeypatch.setenv("SMTP_HOST", "env.relay.example")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_FROM", "env@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    pool = _build_mock_pool([])
    sent = _capture_aiosmtplib(monkeypatch)

    await email_mod.send_magic_link("u@example.com", "https://x/verify?token=t", pool=pool)

    get_secrets_settings.cache_clear()
    assert len(sent) == 1
    call = sent[0]
    assert call["hostname"] == "env.relay.example"
    assert call["port"] == 465
    assert call["use_tls"] is True
    assert call["start_tls"] is False
    assert call["message"]["From"] == "env@example.com"


@pytest.mark.asyncio
async def test_effective_smtp_decrypts_password_correctly(_crypto_key, _no_env_smtp) -> None:
    """smtp.pass round-trips through Fernet decrypt via resolve_secret_row."""
    from jarvis_common.crypto import encrypt_secret

    rows = [
        {"key": "smtp.host", "value": "h", "encrypted_value": None},
        {"key": "smtp.from", "value": "f@example.com", "encrypted_value": None},
        {
            "key": "smtp.pass",
            "value": None,
            "encrypted_value": encrypt_secret("p@ss-w0rd!").encode("ascii"),
        },
    ]
    pool = _build_mock_pool(rows)

    smtp = await email_mod._effective_smtp(pool)
    assert smtp.password == "p@ss-w0rd!"
    assert smtp.host == "h"
    assert smtp.deliverable is True


@pytest.mark.asyncio
async def test_effective_smtp_none_pool_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "envonly.example")
    monkeypatch.setenv("SMTP_FROM", "e@example.com")
    monkeypatch.delenv("SMTP_PORT", raising=False)
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    smtp = await email_mod._effective_smtp(None)
    get_secrets_settings.cache_clear()

    assert smtp.host == "envonly.example"
    assert smtp.port == 587  # default when SMTP_PORT unset
    assert smtp.deliverable is True


@pytest.mark.asyncio
async def test_smtp_configured_true_when_only_db_has_rows(_crypto_key, _no_env_smtp) -> None:
    """_smtp_configured must reflect DB OR env (here: DB only, env empty)."""
    rows = [
        {"key": "smtp.host", "value": "db.host", "encrypted_value": None},
        {"key": "smtp.from", "value": "db@example.com", "encrypted_value": None},
    ]
    pool = _build_mock_pool(rows)
    assert await email_mod._smtp_configured(pool) is True
    # With no pool and no env, it must be False.
    assert await email_mod._smtp_configured(None) is False
