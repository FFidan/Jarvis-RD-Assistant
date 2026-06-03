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
