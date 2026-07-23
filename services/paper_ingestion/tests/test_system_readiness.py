"""Tests for GET /api/system/readiness (release-readiness checklist)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from tests.conftest import FakeRecord, _make_pool_and_conn


def test_smtp_log_only_remediation_does_not_claim_bearer_links_are_logged() -> None:
    """Readiness copy must direct operators to the manual-link recovery path."""
    from paper_ingestion.routers.system_readiness import _DEV_REMEDIATION

    remediation = _DEV_REMEDIATION["dev_smtp_log_only"]
    assert "manual sign-in links" in remediation
    assert "emails print" not in remediation


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
    from jarvis_common.owner import OwnerIdentity
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
    monkeypatch.setattr(
        "paper_ingestion.routers.system_readiness.resolve_owner_identity",
        AsyncMock(return_value=OwnerIdentity("database", "valid", 1)),
        raising=False,
    )
    monkeypatch.setattr(
        "paper_ingestion.routers.system_readiness.visibility_checkpoint_progress",
        AsyncMock(
            return_value={
                "status": "complete",
                "last_chunk_id": 4,
                "total_chunk_id": 4,
            }
        ),
    )
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
        "owner_identity",
        "vector_visibility_metadata",
    } <= names

    # Baseline: dev flags off (green), env amber (development), api_key red
    # (missing) → aggregate is red.
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["dev_auth_bypass"]["status"] == "green"
    assert by_name["environment"]["status"] == "amber"
    assert by_name["api_key"]["status"] == "red"
    assert by_name["audit_log"]["status"] == "green"
    assert by_name["audit_log"]["detail"] == "0 rows"
    assert by_name["owner_identity"]["status"] == "green"
    assert by_name["vector_visibility_metadata"]["status"] == "green"
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
    # Reachability is probed separately; pin it reachable so a configured relay
    # reports green (this test asserts the aggregate, not live relay liveness).
    monkeypatch.setattr(
        "jarvis_common.email.probe_smtp_reachable", AsyncMock(return_value=(True, None))
    )

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
async def test_readiness_vector_visibility_stays_amber_until_current_generation_complete(
    _app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending checkpoint progress is bounded and never exposes its generation token."""
    app, _conn = _app
    generation = "f" * 32
    monkeypatch.setattr(
        "paper_ingestion.routers.system_readiness.visibility_checkpoint_progress",
        AsyncMock(
            return_value={
                "status": "pending",
                "visibility_generation": generation,
                "last_chunk_id": 12,
                "total_chunk_id": 30,
            }
        ),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/system/readiness")

    check = {item["name"]: item for item in response.json()["checks"]}["vector_visibility_metadata"]
    assert check["status"] == "amber"
    assert check["detail"] == "pending: 12/30"
    assert generation not in response.text


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
async def test_readiness_smtp_optional_with_manual_admin_links(_app, monkeypatch):
    """A family instance can invite and recover users without an SMTP relay."""
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
    assert by_name["smtp"]["status"] == "amber", by_name["smtp"]
    copy = f"{by_name['smtp']['detail']} {by_name['smtp']['remediation']}".lower()
    assert "admin" in copy and "manual" in copy and "link" in copy
    assert "stdout" not in copy and "log" not in copy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "state", "expected_words"),
    [
        ("none", "missing", ("jarvis-research", "owner")),
        ("database", "invalid_value", ("jarvis-research", "owner")),
        ("database", "missing_or_deleted_user", ("jarvis-research", "owner")),
        ("database", "non_admin_user", ("jarvis-research", "owner")),
        ("environment", "invalid_value", ("OWNER_USER_ID", "restart")),
    ],
)
async def test_readiness_owner_identity_is_actionable_amber(
    _app, monkeypatch, source, state, expected_words
):
    """Owner recovery problems stay available and point to the right authority."""
    from jarvis_common.owner import OwnerIdentity

    app, _conn = _app
    monkeypatch.setattr(
        "paper_ingestion.routers.system_readiness.resolve_owner_identity",
        AsyncMock(return_value=OwnerIdentity(source, state, 9)),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/readiness")

    assert resp.status_code == 200
    check = {c["name"]: c for c in resp.json()["checks"]}["owner_identity"]
    assert check["status"] == "amber"
    assert check["detail"] == f"{source}: {state}"
    copy = check["remediation"]
    assert all(word in copy for word in expected_words)
    assert "SMTP" not in copy


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
