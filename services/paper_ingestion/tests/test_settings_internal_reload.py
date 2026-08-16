"""Nudge updates remain independent from the Telegram process."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from httpx import ASGITransport
from jarvis_common.testing import SignedIdentityMiddleware
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

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
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

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

    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={
                verify_api_key: lambda: None,
                require_admin: lambda: None,
            },
        ),
    ):
        yield app, conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_update_nudge_makes_no_telegram_request(_app):
    """Telegram discovers Learning-owned schedule changes by periodic refresh."""
    app, conn = _app

    # Prepare DB mocks: fetchrow for existing check, dynamic_update path.
    existing = _make_nudge_record()
    updated = _make_nudge_record(cron_expression="0 10 * * *")
    conn.fetchrow.side_effect = [existing, updated]

    signed_app = SignedIdentityMiddleware(
        app,
        audience="research",
        verifier_app=app,
        user_id=1,
        role="admin",
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=signed_app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/nudges/1",
            json={"cron_expression": "0 10 * * *"},
        )

    assert resp.status_code == 200
    assert len(respx.calls) == 0
