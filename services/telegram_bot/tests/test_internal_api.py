"""Tests for the telegram_bot internal FastAPI app (internal_api.py).

Covers:
- POST /internal/reload-nudges with valid API key → 200, scheduler.reload_nudges called
- POST /internal/reload-nudges with wrong API key → 403
- GET /health (no auth) → 200, {"status": "ok"}
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Path + module stubs (same pattern as conftest.py)
# ---------------------------------------------------------------------------
_SERVICE_ROOT = str(Path(__file__).resolve().parents[1])
if _SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _SERVICE_ROOT)

for _mod_name in (
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
    "uvicorn",
):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app(monkeypatch):
    """Return the internal FastAPI app with a mocked scheduler attached."""
    monkeypatch.setenv("JARVIS_API_KEY", "testkey")
    monkeypatch.setenv("DEV_MODE", "false")

    from app.internal_api import _internal_app

    mock_scheduler = MagicMock()
    mock_scheduler.reload_nudges = AsyncMock(return_value=None)
    _internal_app.state.scheduler = mock_scheduler

    return _internal_app, mock_scheduler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_nudges_valid_key(_app):
    """POST /internal/reload-nudges with correct X-API-Key → 200, scheduler called."""
    app, mock_scheduler = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/internal/reload-nudges",
            headers={"X-API-Key": "testkey"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    mock_scheduler.reload_nudges.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_nudges_bad_key(_app):
    """POST /internal/reload-nudges with wrong X-API-Key → 403."""
    app, mock_scheduler = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/internal/reload-nudges",
            headers={"X-API-Key": "wrong-key"},
        )

    assert resp.status_code == 403
    mock_scheduler.reload_nudges.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_no_auth(_app):
    """GET /health (no auth header) → 200, {"status": "ok"}."""
    app, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok"}
