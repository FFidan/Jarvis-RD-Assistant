"""Contract tests for system.py bypass routes (Sub-wave 4.4 D5).

BYPASS ROUTE CONCERN (Opus W4.1.3 review):
  Three routes in routers/system.py read ``request.app.state.db_pool.acquire()``
  DIRECTLY, bypassing ``Depends(get_db_pool)``:
    - Line 241: GET /api/system/models  (current model assignment fetch)
    - Line 303: GET /api/system/models  (per-machine num_ctx fetch)
    - Line 628: GET /api/system/readiness (audit_log row count)

  The ``pi_test_client`` fixture MUST set BOTH:
    - app.state.db_pool = SharedConnPool(contract_conn)   ← intercepts .state reads
    - app.dependency_overrides[get_db_pool] = lambda: shared  ← intercepts Depends reads

  Without BOTH overrides, system.py routes crash (None.acquire()) or silently
  read from a stale pool at line 241/303/628.

SURVIVOR CITATION:
  verify_api_key branch tests are collapsed into:
    libs/jarvis_common/tests/contract/test_verify_api_key_contract.py

SharedConnPool stmt-cache caveat:
  Routes using ``$1::text`` cast (e.g. ``WHERE key = ANY($1::text[])``) may
  trigger DataError if the stmt cache already holds the statement bound to a
  different type. The audit_log COUNT test below uses ``SELECT COUNT(*) FROM
  audit_log`` — no parameters — so it is safe. The models fetch uses
  ``ANY($1::text[])`` but its result is optional (Exception → issues dict),
  so a DataError there degrades to an "issues" entry and does not fail the
  endpoint. Both behaviours are documented here.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def pi_test_client(contract_conn):
    """Hardened ASGI client for system.py bypass routes.

    Sets BOTH overrides (see module docstring):
      1. app.state.db_pool = shared   — for direct .state.db_pool.acquire() reads
                                        (system.py lines 241, 303, 628)
      2. dependency_overrides[get_db_pool] — for Depends(get_db_pool) reads

    Without override #1, the three bypass routes see None and raise AttributeError.
    Without override #2, Depends-based routes (setup-status, capabilities) use a
    stale/None pool.

    Also bypasses verify_api_key and disables the rate limiter.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None},
            ),
        ):
            async with make_contract_client(app, None) as client:
                yield client
    finally:
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Bypass route: GET /api/system/readiness — line 628 (audit_log count)
#
# This is the clearest single-bypass-route test: the route reads
#   ``async with request.app.state.db_pool.acquire() as conn:
#       row = await conn.fetchrow("SELECT COUNT(*) AS n FROM audit_log")``
# directly on .state.  If the fixture only set dependency_overrides[get_db_pool],
# this line would crash with AttributeError (None.acquire()).
# ---------------------------------------------------------------------------


async def test_readiness_bypass_route_uses_shared_pool(pi_test_client, monkeypatch):
    """GET /api/system/readiness reads audit_log via .state.db_pool (bypass line 628).

    Proves the hardened pi_test_client intercepts direct .state.db_pool.acquire().

    Expected outcome: 200 with audit_log check present and status "green"
    (0 rows in fresh contract DB → green + "0 rows" detail).

    The route is guarded by require_admin_or_api_key; verify_api_key is bypassed
    in the fixture.
    """
    # Ensure no API key is configured so verify_api_key override is the sole path.
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    # Clear settings cache so the env change is visible.
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    resp = await pi_test_client.get("/api/system/readiness")

    assert resp.status_code == 200, (
        f"Expected 200 from readiness route; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "checks" in body
    checks_by_name = {c["name"]: c for c in body["checks"]}
    assert "audit_log" in checks_by_name, (
        "audit_log check missing from readiness response — "
        "bypass route at system.py:628 was not reached"
    )
    # Fresh contract DB has 0 audit_log rows → green with "0 rows" detail.
    audit_check = checks_by_name["audit_log"]
    assert audit_check["status"] == "green", f"Expected audit_log green, got: {audit_check}"
    assert "0 rows" in audit_check["detail"], (
        f"Expected '0 rows' in detail, got: {audit_check['detail']!r}"
    )


async def test_readiness_audit_log_count_reflects_db(pi_test_client, contract_conn, monkeypatch):
    """audit_log count in readiness response reflects actual DB rows (real DB, same txn).

    Inserts a row into audit_log via the contract connection (same txn as the
    ASGI transport), then asserts the count in the response is ≥ 1.

    This test proves that SharedConnPool correctly shares the transactional
    connection: the ASGI route sees the same uncommitted row that the test
    inserted.
    """
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    # Insert a real audit_log row in the same transaction.
    # Schema: (id serial, user_id text, action text, resource text, timestamp, metadata jsonb)
    await contract_conn.execute(
        """INSERT INTO audit_log (action, resource)
           VALUES ('d5_bypass_probe', 'contract_test')"""
    )

    resp = await pi_test_client.get("/api/system/readiness")
    assert resp.status_code == 200

    checks_by_name = {c["name"]: c for c in resp.json()["checks"]}
    audit_check = checks_by_name["audit_log"]
    assert audit_check["status"] == "green"
    # The inserted row is visible in the same transaction.
    count_str = audit_check["detail"]  # e.g. "1 rows"
    count = int(count_str.split()[0])
    assert count >= 1, f"Expected ≥1 audit_log rows (inserted in same txn), got: {count_str!r}"


# ---------------------------------------------------------------------------
# Bypass route: GET /api/system/models — lines 241, 303
#
# This route reads .state.db_pool twice (current model assignment + per-machine
# num_ctx). Both reads are wrapped in try/except so a SharedConnPool stmt-cache
# DataError degrades to an "issues" entry — the route still returns 200.
# We assert the response shape is correct and no 500 is raised.
#
# NOTE: The stmt-cache may trigger a DataError for the $1::text[] cast on a
# warm cache (SharedConnPool limitation). This is documented as a known
# concern. If hit, the route records it under result["issues"]["current"] /
# result["issues"]["..."] and still returns 200. We assert ≥200, <500.
# ---------------------------------------------------------------------------


async def test_models_bypass_routes_return_200(pi_test_client, monkeypatch):
    """GET /api/system/models reaches bypass lines 241 + 303 without crashing.

    The http_client on app.state is needed for the Ollama /api/tags probe;
    we replace it with an AsyncMock to avoid outbound network calls.
    """
    from unittest.mock import AsyncMock, MagicMock
    from paper_ingestion.main import app

    # Provide a mock HTTP client for the Ollama probes inside get_system_models.
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"models": []}

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    original_http = getattr(app.state, "http_client", None)
    app.state.http_client = mock_http

    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    try:
        resp = await pi_test_client.get("/api/system/models")
    finally:
        if original_http is None:
            if hasattr(app.state, "http_client"):
                del app.state.http_client
        else:
            app.state.http_client = original_http

    # Route catches all DB exceptions and returns partial data — never 500.
    assert resp.status_code == 200, (
        f"Expected 200 from /api/system/models; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    # Shape check: these keys are always present regardless of DB/Ollama errors.
    for key in ("status", "installed", "hardware", "current", "issues", "catalog"):
        assert key in body, f"Missing key {key!r} in /api/system/models response"


# ---------------------------------------------------------------------------
# Setup-status: uses Depends(get_db_pool) — NOT a bypass route, but verifies
# the full dual-override fixture works end-to-end.
# ---------------------------------------------------------------------------


async def test_setup_status_via_contract_conn(pi_test_client, monkeypatch):
    """GET /api/system/setup-status reads user_config via Depends(get_db_pool).

    Proves the dependency_overrides[get_db_pool] side of the dual override.
    Fresh contract DB has no user_config rows → setup_completed=False.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # Patch _probe_ollama so the route doesn't try to reach the network.
    import paper_ingestion.routers.system as system_mod

    orig_probe = system_mod._probe_ollama

    async def _fake_probe():
        return False, []

    system_mod._probe_ollama = _fake_probe
    try:
        resp = await pi_test_client.get("/api/system/setup-status")
    finally:
        system_mod._probe_ollama = orig_probe

    assert resp.status_code == 200, f"setup-status failed: {resp.text}"
    body = resp.json()
    assert body["setup_completed"] is False
    assert body["telegram_configured"] is False
    assert body["topics_count"] == 0


# ---------------------------------------------------------------------------
# Phase B additions — setup-status topics_count reflects real DB rows
# ---------------------------------------------------------------------------


# §A-SYS-05 — GET /api/system/setup-status: topics_count reflects real topics table
# Verified: system.py:193 (SELECT COUNT(*) AS n FROM topics)


async def test_setup_status_topics_count_reflects_real_db(
    pi_test_client, contract_conn, monkeypatch
):
    """GET /api/system/setup-status topics_count reflects the real topics table row count.

    Inserts a topic row in the same transaction as the ASGI request (shared
    contract_conn) and asserts topics_count >= 1.  Proves the dual-override
    fixture shares the transactional connection for the topics COUNT query.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    import paper_ingestion.routers.system as system_mod

    # Insert a topic inside the contract transaction — visible to the ASGI route
    await contract_conn.execute(
        "INSERT INTO topics (name, query_terms) VALUES ('sys-contract-topic', ARRAY['test'])"
    )

    orig_probe = system_mod._probe_ollama

    async def _fake_probe():
        return False, []

    system_mod._probe_ollama = _fake_probe
    try:
        resp = await pi_test_client.get("/api/system/setup-status")
    finally:
        system_mod._probe_ollama = orig_probe

    assert resp.status_code == 200, f"setup-status failed: {resp.text}"
    body = resp.json()
    assert body["topics_count"] >= 1, (
        f"topics_count must be >= 1 after inserting a topic in the same txn; "
        f"got {body['topics_count']} — SharedConnPool may not be sharing the txn connection"
    )


# §A-SYS-06 — GET /api/system/setup-status: setup_completed=True when user_config set
# Verified: system.py:196 (_coerce_bool(config.get("setup.completed"), default=False))


async def test_setup_status_setup_completed_when_config_set(
    pi_test_client, contract_conn, monkeypatch
):
    """GET /api/system/setup-status returns setup_completed=True when user_config row exists.

    Inserts 'setup.completed' = 'true' (global, user_id IS NULL) in the same
    transaction as the ASGI request. Proves the real user_config read path.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    import paper_ingestion.routers.system as system_mod

    await contract_conn.execute(
        """INSERT INTO user_config (key, value, user_id)
           VALUES ('setup.completed', 'true'::jsonb, NULL)
           ON CONFLICT (key, user_id) DO UPDATE SET value = 'true'::jsonb
           WHERE user_config.user_id IS NULL"""
    )

    orig_probe = system_mod._probe_ollama

    async def _fake_probe():
        return False, []

    system_mod._probe_ollama = _fake_probe
    try:
        resp = await pi_test_client.get("/api/system/setup-status")
    finally:
        system_mod._probe_ollama = orig_probe

    assert resp.status_code == 200
    body = resp.json()
    assert body["setup_completed"] is True, (
        f"setup_completed must be True when user_config 'setup.completed'='true'; "
        f"got {body['setup_completed']}"
    )
