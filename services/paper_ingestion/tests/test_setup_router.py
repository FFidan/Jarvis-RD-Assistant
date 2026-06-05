"""Tests for SetupStatusResponse hw_tier / backend extensions (Task 18)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import paper_ingestion.routers.setup as setup_router
import pytest
from fastapi import HTTPException
from jarvis_common.testing import make_pool_and_conn


@pytest.fixture(autouse=True)
def _disable_limiter():
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


def _build_request(pool: MagicMock) -> SimpleNamespace:
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app, state=state, cookies={})


@pytest.mark.asyncio
async def test_system_check_requires_admin_when_configured() -> None:
    """system_check must raise 403 when an admin exists and caller is not admin."""
    from fastapi import HTTPException

    # admin_count > 0 → setup is complete; caller has no role (unauthenticated)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # 1 admin exists
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)
    # request.state has no user_role → non-admin caller

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.system_check(request)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_setup_status_includes_hw_fields(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")
    monkeypatch.setenv("JARVIS_LLM_BACKEND", "vllm")

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)

    assert res.hw_tier_baseline == "ge-48"
    assert res.hw_tier_current is not None
    assert res.current_backend == "vllm"


@pytest.mark.asyncio
async def test_setup_status_reports_effective_backend_when_unset(monkeypatch) -> None:
    """With no JARVIS_LLM_BACKEND override, current_backend reports the effective
    runtime default ('ollama'), not null (OPS-01)."""
    monkeypatch.delenv("JARVIS_LLM_BACKEND", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)

    assert res.current_backend == "ollama"


@pytest.mark.asyncio
async def test_setup_status_returns_503_on_db_failure() -> None:
    """get_status must raise HTTP 503 when the DB query fails (fail-closed; MED-PI-02)."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=asyncpg.PostgresError("connection lost"))
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.get_status(request)

    assert exc_info.value.status_code == 503
    assert "Setup status check failed" in exc_info.value.detail


# ---------------------------------------------------------------------------
# PI-AUTH-02: first-admin creation log must not contain raw email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_first_admin_logs_hash_not_raw_email(monkeypatch, caplog) -> None:
    """logger.info on first-admin creation must record email_hash, never the raw address."""
    import hashlib
    import logging

    from fastapi import Response

    raw_email = "admin@example.com"
    expected_hash = hashlib.sha256(raw_email.encode("utf-8")).hexdigest()

    user_row = {"id": 42, "email": raw_email, "role": "admin"}
    conn = AsyncMock()
    # pool.acquire() is called twice: once by require_unconfigured_or_admin (_admin_count),
    # once by the handler body.  Both share the same conn mock.
    # require_unconfigured_or_admin: conn.fetchval → admin_count=0 (bootstrap mode)
    # handler body (inside transaction):
    #   conn.execute  → advisory lock (returns None)
    #   conn.fetchval → admin_count=0 (inner guard)
    #   conn.fetchrow → existing check → None
    #   conn.fetchrow → INSERT RETURNING → user_row
    #   conn.fetchval → session INSERT RETURNING id → 99
    conn.execute = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(side_effect=[0, 0, 99])  # outer count, inner count, session_id
    conn.fetchrow = AsyncMock(side_effect=[None, user_row])  # no existing, INSERT row

    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)
    response = Response()

    body = setup_router.AdminBody(email=raw_email)

    with caplog.at_level(logging.INFO, logger="paper_ingestion.routers.setup"):
        await setup_router.create_first_admin(body, request, response)

    assert any(expected_hash in r.message for r in caplog.records), (
        "Expected email hash in log record"
    )
    assert not any(raw_email in r.message for r in caplog.records), (
        "Raw email must not appear in any log record"
    )
