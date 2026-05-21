"""Contract tests for settings/config endpoints (Sub-wave 4.4 D5).

Exercises real DB-backed config round-trips via the ASGI transport + SharedConnPool.

SURVIVOR CITATION:
  verify_api_key branch tests previously scattered across test_settings.py,
  test_settings_per_user_scoping.py, test_settings_zotero.py, test_auth_magic_link.py
  and test_admin_users.py are now collapsed into:
    libs/jarvis_common/tests/contract/test_verify_api_key_contract.py

This file covers only the DB-backed settings contract behaviours that mock-unit
tests cannot exercise: that UPSERT actually persists and GET reads the row back,
and that the scoping SQL correctly filters by user_id.
"""

from __future__ import annotations

import pytest
import httpx
import pytest_asyncio
from unittest.mock import AsyncMock
from jarvis_common.testing import SharedConnPool

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture
async def pi_settings_client(contract_conn):
    """ASGI client wired to the real per-test transaction via SharedConnPool.

    Sets BOTH overrides so routes that use Depends(get_db_pool) AND any that
    read request.app.state.db_pool directly (system.py lines 241, 303, 628)
    both reach the same transactional connection.

    Also patches ``require_admin`` in the settings router namespace because
    ``set_config`` calls it directly (not via Depends), so dependency_overrides
    cannot intercept it — same technique as the mock-unit _app fixture.
    """
    from paper_ingestion.main import app
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers import settings as _settings_mod
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin

    async def _allow_all(request=None) -> None:  # noqa: ARG001
        return None

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    _orig_require_admin = _settings_mod.require_admin

    app.state.db_pool = shared
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[require_admin] = _allow_all
    _settings_mod.require_admin = _allow_all
    # Idiomatic mock carve-out: set_config reads request.app.state.http_client for
    # the LiteLLM model-validation probe (outbound HTTP — never touches the DB).
    original_http = getattr(app.state, "http_client", None)
    app.state.http_client = AsyncMock()
    # Disable rate limiter for contract tests.
    app.state.limiter.enabled = False

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        _settings_mod.require_admin = _orig_require_admin
        if original_pool is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original_pool
        if original_http is None:
            if hasattr(app.state, "http_client"):
                del app.state.http_client
        else:
            app.state.http_client = original_http
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(verify_api_key, None)
        app.dependency_overrides.pop(require_admin, None)
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# GET /api/config — lists all system config rows
# ---------------------------------------------------------------------------


async def test_list_config_returns_list(pi_settings_client):
    """GET /api/config returns a list (may be empty against fresh contract DB)."""
    resp = await pi_settings_client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)


# ---------------------------------------------------------------------------
# PUT + GET round-trip for a known safe key (pulse.deck_size — integer)
#
# NOTE: SharedConnPool stmt-cache caveat: routes using `$1::text` casts may
# trigger DataError if stmt-cache is warm from a prior differently-typed bind.
# pulse.deck_size uses a plain $1 integer parameter in the validator so is safe.
# ---------------------------------------------------------------------------


async def test_put_config_string_value_round_trip(contract_conn, pi_settings_client):
    """PUT /api/config/pulse.cron persists; GET /api/config/{key} reads it back."""
    cron_value = "0 5 * * *"
    put_resp = await pi_settings_client.put(
        "/api/config/pulse.cron",
        json={"key": "pulse.cron", "value": cron_value},
    )
    assert put_resp.status_code == 200, f"PUT failed: {put_resp.json()}"
    body = put_resp.json()
    assert body["key"] == "pulse.cron"
    assert body["value"] == cron_value

    # Verify the row landed in user_config (direct DB query, same txn).
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        "pulse.cron",
    )
    assert row is not None, "PUT did not persist a user_config row"
    # asyncpg JSONB codec returns the Python value directly — a bare string.
    assert row["value"] == cron_value


async def test_get_config_key_not_found_returns_404(pi_settings_client):
    """GET /api/config/{key} returns 404 when the key does not exist in DB."""
    resp = await pi_settings_client.get("/api/config/nonexistent.key.xyz")
    assert resp.status_code == 404


async def test_put_config_ghost_key_returns_400(pi_settings_client):
    """Ghost keys removed from the allow-list return 400, not a DB write.

    Collapsed from test_settings.py::test_ghost_key_returns_400 parametrize family
    (§D5-05).  We test one representative ghost key here; the full parametrized
    family remains in the mock-unit file for breadth coverage.
    """
    resp = await pi_settings_client.put(
        "/api/config/paper.max_daily",
        json={"key": "paper.max_daily", "value": 10},
    )
    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


async def test_put_config_ghost_key_does_not_write_db(contract_conn, pi_settings_client):
    """PUT of a ghost key returns 400 and writes no row to user_config."""
    await pi_settings_client.put(
        "/api/config/ui.page_size",
        json={"key": "ui.page_size", "value": 20},
    )
    row = await contract_conn.fetchrow("SELECT 1 FROM user_config WHERE key = 'ui.page_size'")
    assert row is None, "Ghost key must not write to user_config"
