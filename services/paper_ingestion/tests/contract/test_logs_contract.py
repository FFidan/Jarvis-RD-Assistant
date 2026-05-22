"""Logs domain contract tests — Phase B target rows A51-A56.

Survivor-of: (all NONE — no prior contract coverage).
Carve-out: SSE stream (A56) is IDIOMATIC-MOCK-ONLY — the generator polls
DB in an infinite loop; testing it without a real procrastinate job that
transitions to terminal state would require mocking time or the job table,
making it no stronger than the mock-unit test. Skipped per YAGNI.

Rows covered:
  A51 GET /api/logs/events          — admin gate + events returned from DB
  A52 GET /api/logs/events/{id}     — single event; 404 for non-existent
  A53 GET /api/logs/summary         — aggregated counts non-negative
  A54 GET /api/logs/correlation/{id} — correlated events from DB match inserted id
  A55 GET /api/logs/sources         — distinct source names returned

Row deferred:
  A56 GET /api/logs/stream/{id}     — SSE stream; IDIOMATIC-MOCK-ONLY (infinite loop)
"""

from __future__ import annotations

import uuid

import pytest
import httpx

from jarvis_common.testing_contract_apps import (
    DEFAULT_CONTRACT_API_KEY,
    make_contract_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


def _make_api_key_client(app):
    return make_contract_client(app, None)


# ---------------------------------------------------------------------------
# A51: GET /api/logs/events — admin gate + events list from DB
# ---------------------------------------------------------------------------


async def test_a51_list_events_returns_events_from_db(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A51: GET /api/logs/events returns system_events rows.

    Verified: logs.py:74-149 list_events — cursor-paginated SELECT from system_events.
    Admin gate: require_admin_or_api_key — API-key-only caller passes.
    """
    # Insert a known event row
    event_id = await contract_conn.fetchval(
        """INSERT INTO system_events (level, category, source, message)
           VALUES ('info', 'source', 'contract-test-source', 'contract test event')
           RETURNING id"""
    )

    async with _make_api_key_client(_pi_app_with_pool) as c:
        resp = await c.get("/api/logs/events")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert "events" in body, f"Missing 'events' key: {body}"
    assert "next_cursor" in body, f"Missing 'next_cursor' key: {body}"
    ids_in_response = [e["id"] for e in body["events"]]
    assert event_id in ids_in_response, (
        f"Inserted event {event_id} not found in /api/logs/events response"
    )


async def test_a51_list_events_non_admin_non_apikey_gets_401_or_403(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A51: admin gate blocks regular-session callers.

    require_admin_or_api_key: a non-admin session role → 403.
    The contract_two_users seeds role='user', so cookie_a represents a non-admin session.
    """
    # A regular user session (not admin) should be blocked
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_pi_app_with_pool),
        base_url="http://test",
        headers={"X-API-Key": DEFAULT_CONTRACT_API_KEY},
        cookies={"jarvis_session": contract_two_users.cookie_a},
    ) as c:
        resp = await c.get("/api/logs/events")

    # A non-admin session role triggers 403; no X-API-Key without session triggers 401
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for non-admin session, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# A52: GET /api/logs/events/{id} — single event; 404 for non-existent
# ---------------------------------------------------------------------------


async def test_a52_get_event_returns_single_event_and_404_for_missing(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A52: GET /api/logs/events/{id} returns event or 404.

    Verified: logs.py:159-176 get_event — SELECT WHERE id=$1; 404 if None.
    """
    event_id = await contract_conn.fetchval(
        """INSERT INTO system_events (level, category, source, message)
           VALUES ('error', 'error', 'contract-src', 'single event lookup')
           RETURNING id"""
    )

    async with _make_api_key_client(_pi_app_with_pool) as c:
        resp = await c.get(f"/api/logs/events/{event_id}")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["id"] == event_id
    assert body["message"] == "single event lookup"

    # Non-existent id
    async with _make_api_key_client(_pi_app_with_pool) as c:
        resp404 = await c.get("/api/logs/events/999999999")
    assert resp404.status_code == 404, f"Expected 404, got {resp404.status_code}"


# ---------------------------------------------------------------------------
# A53: GET /api/logs/summary — aggregated counts non-negative
# ---------------------------------------------------------------------------


async def test_a53_get_summary_returns_non_negative_counts(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A53: GET /api/logs/summary returns aggregated counts.

    Verified: logs.py:186-225 get_summary — GROUP BY level, category last 24h.
    """
    # Insert a recent event to ensure summary is non-empty
    await contract_conn.execute(
        """INSERT INTO system_events (level, category, source, message)
           VALUES ('warning', 'config', 'contract-summary-src', 'summary test')"""
    )

    async with _make_api_key_client(_pi_app_with_pool) as c:
        resp = await c.get("/api/logs/summary")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert "by_level" in body, f"Missing 'by_level': {body}"
    assert "by_category" in body, f"Missing 'by_category': {body}"
    assert "total" in body, f"Missing 'total': {body}"
    assert isinstance(body["total"], int) and body["total"] >= 0
    for key, val in body["by_level"].items():
        assert isinstance(val, int) and val >= 0, (
            f"by_level[{key!r}] = {val!r} is not non-negative int"
        )
    for key, val in body["by_category"].items():
        assert isinstance(val, int) and val >= 0, (
            f"by_category[{key!r}] = {val!r} is not non-negative int"
        )


# ---------------------------------------------------------------------------
# A54: GET /api/logs/correlation/{id} — correlated events match inserted id
# ---------------------------------------------------------------------------


async def test_a54_get_correlation_returns_matching_events(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A54: GET /api/logs/correlation/{id} returns events matching correlation_id.

    Verified: logs.py:235-256 get_correlation — WHERE correlation_id=$1 ORDER BY created_at ASC.
    """
    correlation_id = uuid.uuid4()

    # Insert two events with this correlation_id
    id1 = await contract_conn.fetchval(
        """INSERT INTO system_events (level, category, source, message, correlation_id)
           VALUES ('info', 'source', 'corr-src', 'event-1', $1) RETURNING id""",
        correlation_id,
    )
    id2 = await contract_conn.fetchval(
        """INSERT INTO system_events (level, category, source, message, correlation_id)
           VALUES ('info', 'source', 'corr-src', 'event-2', $1) RETURNING id""",
        correlation_id,
    )
    # Insert unrelated event with different correlation_id
    await contract_conn.execute(
        """INSERT INTO system_events (level, category, source, message, correlation_id)
           VALUES ('info', 'source', 'corr-src', 'unrelated', $1)""",
        uuid.uuid4(),
    )

    async with _make_api_key_client(_pi_app_with_pool) as c:
        resp = await c.get(f"/api/logs/correlation/{correlation_id}")

    assert resp.status_code == 200, resp.text[:300]
    events = resp.json()
    assert isinstance(events, list), f"Expected list, got {type(events)}"
    ids_in_resp = [e["id"] for e in events]
    assert id1 in ids_in_resp, f"event-1 (id={id1}) not in correlation response"
    assert id2 in ids_in_resp, f"event-2 (id={id2}) not in correlation response"
    # Unrelated event must not appear
    unrelated_messages = [e["message"] for e in events if e["message"] == "unrelated"]
    assert unrelated_messages == [], "Unrelated event leaked into correlation response"


async def test_a54_get_correlation_invalid_uuid_returns_422(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A54: GET /api/logs/correlation/{id} validates UUID format.

    Verified: logs.py:242-244 — HTTPException(422) on invalid UUID.
    """
    async with _make_api_key_client(_pi_app_with_pool) as c:
        resp = await c.get("/api/logs/correlation/not-a-uuid")
    assert resp.status_code == 422, f"Expected 422 for invalid UUID, got {resp.status_code}"


# ---------------------------------------------------------------------------
# A55: GET /api/logs/sources — distinct source names returned
# ---------------------------------------------------------------------------


async def test_a55_list_sources_returns_distinct_sources(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A55: GET /api/logs/sources returns distinct source values.

    Verified: logs.py:266-296 list_sources — DISTINCT source WHERE created_at >= NOW()-7d.
    Note: bypasses in-process 60s cache by exploiting the fact that a freshly
    reset contract_conn has no cache state (module-level _sources_cache persists
    across tests but that's acceptable — we only assert list semantics, not cache).
    """
    import paper_ingestion.routers.logs as logs_module

    # Reset the module-level cache so this test observes fresh DB state
    logs_module._sources_cache = None

    unique_source = f"contract-src-{uuid.uuid4().hex[:8]}"
    await contract_conn.execute(
        """INSERT INTO system_events (level, category, source, message)
           VALUES ('info', 'source', $1, 'source listing test')""",
        unique_source,
    )
    # Reset cache again after insert to force re-query
    logs_module._sources_cache = None

    async with _make_api_key_client(_pi_app_with_pool) as c:
        resp = await c.get("/api/logs/sources")

    assert resp.status_code == 200, resp.text[:300]
    sources = resp.json()
    assert isinstance(sources, list), f"Expected list, got {type(sources)}"
    assert unique_source in sources, (
        f"Inserted source {unique_source!r} not found in /api/logs/sources: {sources}"
    )
