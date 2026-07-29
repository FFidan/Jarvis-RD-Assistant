"""Tests for PUT /api/nudges/{id} — verifies telegram_bot internal reload is triggered.

Covers:
- test_update_nudge_fires_reload: asserts correct URL + X-API-Key header sent to
  TELEGRAM_BOT_URL/internal/reload-nudges after a nudge update.
- test_update_nudge_swallows_connection_refused: ConnectError from the outbound call
  is swallowed; handler still returns 200.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers (reuse conftest FakeRecord + _make_pool_and_conn)
# ---------------------------------------------------------------------------


def _make_nudge_record(**kwargs) -> dict:
    defaults = {
        "id": 1,
        "nudge_type": "review_reminder",
        "cron_expression": "0 9 * * *",
        "enabled": True,
        "config": {},
        "last_fired_at": None,
        "created_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# Fixture: minimal paper_ingestion app with mocked DB + auth disabled
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal paper_ingestion app with mocked DB and auth disabled."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    # The conftest.py provides _make_pool_and_conn as a shared helper but we
    # inline it here to keep the fixture self-contained.
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    app.state.db_pool = pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # Admin-gate the settings endpoints (see test_settings._app).
    app.dependency_overrides[require_admin] = lambda: None
    _orig_require_admin = _settings_mod.require_admin
    _settings_mod.require_admin = AsyncMock(return_value=None)

    yield app, conn
    _settings_mod.require_admin = _orig_require_admin
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_update_nudge_fires_reload(_app, monkeypatch):
    """PUT /api/nudges/{id} sends POST to TELEGRAM_BOT_URL/internal/reload-nudges
    with the correct X-API-Key header."""
    app, conn = _app

    # Wire TELEGRAM_BOT_URL and JARVIS_API_KEY so the handler picks them up.
    bot_url = "http://telegram_bot_test:8002"
    monkeypatch.setenv("TELEGRAM_BOT_URL", bot_url)
    # Set JARVIS_API_KEY via env — read inline at call time, not captured at import.
    monkeypatch.setenv("JARVIS_API_KEY", "testkey")

    # Mock the outbound reload call.
    reload_route = respx.post(f"{bot_url}/internal/reload-nudges").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    # Prepare DB mocks: fetchrow for existing check, dynamic_update path.
    existing = _make_nudge_record()
    updated = _make_nudge_record(cron_expression="0 10 * * *")
    conn.fetchrow.side_effect = [existing, updated]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/nudges/1",
            json={"cron_expression": "0 10 * * *"},
        )

    assert resp.status_code == 200

    # Verify the reload was called exactly once.
    assert reload_route.called, "Expected POST to telegram_bot internal reload endpoint"

    # Verify the correct X-API-Key header was sent.
    outbound_request = reload_route.calls.last.request
    assert outbound_request.headers.get("x-api-key") == "testkey"


@respx.mock
@pytest.mark.asyncio
async def test_update_nudge_swallows_connection_refused(_app, monkeypatch):
    """PUT /api/nudges/{id} returns 200 even when telegram_bot is unreachable.

    The handler wraps the outbound call in contextlib.suppress(Exception),
    so ConnectError must not bubble up.
    """
    app, conn = _app

    bot_url = "http://telegram_bot_test:8002"
    monkeypatch.setenv("TELEGRAM_BOT_URL", bot_url)
    monkeypatch.setenv("JARVIS_API_KEY", "testkey")

    # Make the outbound reload call raise ConnectError (simulates connection refused).
    respx.post(f"{bot_url}/internal/reload-nudges").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    existing = _make_nudge_record()
    updated = _make_nudge_record(enabled=False)
    conn.fetchrow.side_effect = [existing, updated]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/nudges/1",
            json={"enabled": False},
        )

    # ConnectError must be swallowed; response must still be 200.
    assert resp.status_code == 200
