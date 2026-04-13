"""Tests verifying that the recommendations list endpoint enforces rate limits."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

# Stub heavy native modules unavailable outside Docker.
for _mod_name in ("fitz",):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402


@pytest.fixture()
def _app(monkeypatch):
    """App fixture with db pool mocked and rate limiter ENABLED."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    from app.main import app, get_db_pool
    from jarvis_common import verify_api_key

    # Build a mock pool that returns an empty rows list for fetch().
    conn = AsyncMock()
    conn.fetch.return_value = []
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = ctx

    app.state.db_pool = mock_pool
    # Keep limiter ENABLED (do NOT set app.state.limiter.enabled = False)
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_recommendations_rate_limit(_app):
    """6 rapid calls to GET /api/recommendations should yield at least one 429."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://test"
    ) as client:
        statuses = []
        for _ in range(6):
            resp = await client.get("/api/recommendations")
            statuses.append(resp.status_code)

    assert 429 in statuses, f"Expected at least one 429 in {statuses}"
