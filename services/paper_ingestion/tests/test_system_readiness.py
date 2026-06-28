"""Tests for GET /api/system/readiness (pre-public-launch checklist)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from tests.conftest import FakeRecord, _make_pool_and_conn


@pytest.fixture()
def _app(monkeypatch):
    # Deterministic baseline: no dev flags, no configured API key/SMTP.
    for var in (
        "DEV_MODE",
        "DEV_AUTH_BYPASS",
        "DEV_ERROR_DETAIL",
        "DEV_CORS_OPEN",
        "DEV_SMTP_LOG_ONLY",
        "DEV_CRYPTO_RELAXED",
        "JARVIS_API_KEY",
        "SMTP_HOST",
        "ENVIRONMENT",
    ):
        monkeypatch.delenv(var, raising=False)

    from jarvis_common import verify_api_key
    from jarvis_common.settings import get_secrets_settings
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    get_secrets_settings.cache_clear()

    mock_pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(n=0)
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True
    get_secrets_settings.cache_clear()


@pytest.mark.asyncio
async def test_readiness_shape_and_baseline(_app):
    app, _conn = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/readiness")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"status", "checks"}
    assert isinstance(body["checks"], list)
    for check in body["checks"]:
        assert set(check.keys()) == {"name", "status", "detail", "remediation"}
        assert check["status"] in {"green", "amber", "red"}

    names = {c["name"] for c in body["checks"]}
    assert {
        "dev_auth_bypass",
        "dev_error_detail",
        "dev_cors_open",
        "dev_smtp_log_only",
        "dev_crypto_relaxed",
        "environment",
        "api_key",
        "smtp",
        "https",
        "audit_log",
    } <= names

    # Baseline: dev flags off (green), env amber (development), api_key red
    # (missing) → aggregate is red.
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["dev_auth_bypass"]["status"] == "green"
    assert by_name["environment"]["status"] == "amber"
    assert by_name["api_key"]["status"] == "red"
    assert by_name["audit_log"]["status"] == "green"
    assert by_name["audit_log"]["detail"] == "0 rows"
    assert body["status"] == "red"


@pytest.mark.asyncio
async def test_readiness_dev_flag_true_yields_red(_app, monkeypatch):
    app, _conn = _app
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/readiness")

    assert resp.status_code == 200
    body = resp.json()
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["dev_auth_bypass"]["status"] == "red"
    assert by_name["dev_auth_bypass"]["detail"] == "enabled"
    assert body["status"] == "red"


@pytest.mark.asyncio
async def test_readiness_all_green_aggregate(_app, monkeypatch):
    """Production-shaped config with HTTPS proxy header → aggregate green."""
    app, _conn = _app
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 40)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")

    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/system/readiness",
            headers={"x-forwarded-proto": "https"},
        )

    assert resp.status_code == 200
    body = resp.json()
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["environment"]["status"] == "green"
    assert by_name["api_key"]["status"] == "green"
    assert by_name["api_key"]["detail"] == "configured (>=32 chars)"
    assert by_name["smtp"]["status"] == "green"
    assert by_name["https"]["status"] == "green"
    assert by_name["https"]["detail"] == "https"
    assert body["status"] == "green"
    # The key value must never be echoed back.
    assert "x" * 40 not in resp.text


@pytest.mark.asyncio
async def test_readiness_smtp_amber_when_username_without_password(_app, monkeypatch):
    """host+sender present but SMTP_USER set with no SMTP_PASS → readiness SMTP must not be green.

    The relay would 535 at AUTH, so a half-configured login must surface as amber,
    matching the Settings banner (effective_smtp_status), not report green.
    """
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("SMTP_USER", "relay-user")
    monkeypatch.delenv("SMTP_PASS", raising=False)

    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()
    app, _conn = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/readiness")

    assert resp.status_code == 200
    by_name = {c["name"]: c for c in resp.json()["checks"]}
    assert by_name["smtp"]["status"] == "amber", (
        f"username-without-password must not be green; got {by_name['smtp']}"
    )
    # The remediation/detail must never echo the configured value.
    assert "relay-user" not in resp.text
    get_secrets_settings.cache_clear()


@pytest.mark.asyncio
async def test_readiness_smtp_red_on_production_multiuser(_app, monkeypatch):
    """Production + >1 user + no deliverable SMTP → smtp readiness is red, not amber.

    On a multi-user production box magic-link is the only login path for
    non-owner users, so a missing relay is a hard failure.
    """
    app, conn = _app
    monkeypatch.setenv("ENVIRONMENT", "production")
    # >1 non-deleted user → the box is multi-user.
    conn.fetchval = AsyncMock(return_value=2)

    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/readiness")

    assert resp.status_code == 200
    by_name = {c["name"]: c for c in resp.json()["checks"]}
    assert by_name["smtp"]["status"] == "red", by_name["smtp"]
    assert resp.json()["status"] == "red"


@pytest.mark.asyncio
async def test_readiness_requires_auth(monkeypatch):
    """Without a valid X-API-Key the endpoint must 401 (global verify_api_key)."""
    monkeypatch.setenv("JARVIS_API_KEY", "secret-key-value-1234567890")

    from jarvis_common.auth import refresh_api_key_cache
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    refresh_api_key_cache()
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(n=0)
    app.state.db_pool = pool
    old_limiter = app.state.limiter.enabled
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/system/readiness")
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = old_limiter
        monkeypatch.delenv("JARVIS_API_KEY", raising=False)
        refresh_api_key_cache()

    assert resp.status_code in (401, 403)
