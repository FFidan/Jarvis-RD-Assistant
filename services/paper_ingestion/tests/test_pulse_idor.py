"""Tests for IDOR guards on Pulse router endpoints (H2 / WS6-A1b).

Verifies that:
- explain_card SQL contains ``IS NOT DISTINCT FROM`` for user_id ownership filter.
- rate_card deck-guard SQL contains ``pd.user_id IS NOT DISTINCT FROM`` bound to
  the caller's user_id.

Uses the same recording-mock pattern as test_pulse_router.py / conftest.py:
  _make_pool_and_conn() returns an AsyncMock conn whose .fetchrow / .fetchval
  calls record the SQL + args so we can assert on them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common import current_user_id, current_user_id_or_none, verify_api_key
from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Shared fixture: minimal FastAPI app mounting only the pulse router
# ---------------------------------------------------------------------------


def _make_client(user_id_override=None):
    """Return (TestClient, pool, conn) with limiter disabled and auth stubbed.

    Parameters
    ----------
    user_id_override:
        Value that both ``current_user_id`` and ``current_user_id_or_none``
        dependencies will return.  Defaults to None (single-tenant stub mode).
    """
    from paper_ingestion.deps import limiter
    from paper_ingestion.routers import pulse as pulse_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()

    app.include_router(pulse_router.router)

    # Stub auth
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id] = lambda: user_id_override
    app.dependency_overrides[current_user_id_or_none] = lambda: user_id_override

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, pool, conn, app


# ---------------------------------------------------------------------------
# Test 1: explain_card SQL must contain IS NOT DISTINCT FROM
# ---------------------------------------------------------------------------


def test_explain_card_filters_by_user_id_in_not_distinct_form():
    """GET /explain/{card_id} must pass user_id via IS NOT DISTINCT FROM.

    The SQL captured by conn.fetchrow must include the ownership filter so that
    sequential card-id enumeration (IDOR) is blocked.
    """
    tc, pool, conn, app = _make_client(user_id_override=None)

    conn.fetchrow.return_value = FakeRecord(
        {
            "id": 7,
            "reasoning": "very relevant to ML",
            "signals": {"embedding": 0.9, "topic": 0.7},
            "llm_relevance": 8,
            "llm_novelty": 6,
        }
    )

    try:
        resp = tc.get("/api/pulse/explain/7")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"

    # Assert that the SQL used for the fetchrow call contains the ownership guard
    assert conn.fetchrow.await_count >= 1, "conn.fetchrow was never awaited"
    call_args = conn.fetchrow.await_args
    assert call_args is not None

    sql: str = call_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql, (
        f"Expected 'IS NOT DISTINCT FROM' in explain_card SQL but got:\n{sql!r}"
    )

    # user_id (None in stub mode) must be passed as $2 parameter
    params = call_args.args[1:]  # (card_id, user_id)
    assert len(params) == 2, (
        f"Expected 2 positional params (card_id, user_id), got {len(params)}: {params}"
    )
    card_id_param, user_id_param = params
    assert card_id_param == 7
    assert user_id_param is None  # stub mode returns None


def test_explain_card_filters_by_real_user_id():
    """When a real user_id is provided, $2 must be bound to that integer."""
    tc, pool, conn, app = _make_client(user_id_override=42)

    conn.fetchrow.return_value = FakeRecord(
        {
            "id": 3,
            "reasoning": "test",
            "signals": {},
            "llm_relevance": 5,
            "llm_novelty": 4,
        }
    )

    try:
        resp = tc.get("/api/pulse/explain/3")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200

    call_args = conn.fetchrow.await_args
    assert call_args is not None
    params = call_args.args[1:]
    assert len(params) == 2
    assert params[0] == 3  # card_id
    assert params[1] == 42  # user_id bound to $2


# ---------------------------------------------------------------------------
# Test 2: rate_card deck-guard SQL must contain pd.user_id IS NOT DISTINCT FROM
# ---------------------------------------------------------------------------


def test_rate_card_deck_guard_filters_by_user_id():
    """POST /rate deck-membership guard must include pd.user_id IS NOT DISTINCT FROM.

    Without this filter any user_id could rate any other user's deck-paper (IDOR).
    The SQL captured by conn.fetchval must include the ownership predicate.
    """
    tc, pool, conn, app = _make_client(user_id_override=None)

    # fetchval returns truthy value → paper is in the deck; execute is a no-op
    conn.fetchval.return_value = 1
    conn.execute.return_value = "INSERT 0 1"

    try:
        resp = tc.post("/api/pulse/rate", json={"paper_id": 42, "rating": "up"})
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"

    # conn.fetchval is the deck-membership guard call
    assert conn.fetchval.await_count >= 1, "conn.fetchval was never awaited"
    call_args = conn.fetchval.await_args
    assert call_args is not None

    sql: str = call_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql, (
        f"Expected 'IS NOT DISTINCT FROM' in deck-guard SQL but got:\n{sql!r}"
    )
    assert "pd.user_id" in sql, f"Expected 'pd.user_id' in deck-guard SQL but got:\n{sql!r}"

    # The guard query must pass (paper_id, user_id) — $1 and $2
    params = call_args.args[1:]
    assert len(params) == 2, (
        f"Expected 2 params (paper_id, user_id) in deck-guard, got {len(params)}: {params}"
    )
    paper_id_param, user_id_param = params
    assert paper_id_param == 42
    assert user_id_param is None  # stub mode


def test_rate_card_deck_guard_with_real_user_id():
    """When user_id=7 is injected, $2 in the deck guard must be 7."""
    tc, pool, conn, app = _make_client(user_id_override=7)

    conn.fetchval.return_value = 1
    conn.execute.return_value = "INSERT 0 1"

    try:
        resp = tc.post("/api/pulse/rate", json={"paper_id": 99, "rating": "down"})
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200

    call_args = conn.fetchval.await_args
    assert call_args is not None
    params = call_args.args[1:]
    assert len(params) == 2
    assert params[0] == 99  # paper_id
    assert params[1] == 7  # user_id bound to $2


# ---------------------------------------------------------------------------
# Test 3: rate_card preference no-clobber (B3)
# ---------------------------------------------------------------------------


def _capture_rate_calls(rating: str) -> list:
    """Issue a rate_card POST and return the conn.execute await call list."""
    tc, pool, conn, app = _make_client(user_id_override=None)
    conn.fetchval.return_value = 1
    conn.execute.return_value = "INSERT 0 1"
    try:
        resp = tc.post("/api/pulse/rate", json={"paper_id": 1, "rating": rating})
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True
    assert resp.status_code == 200
    return list(conn.execute.await_args_list)


def test_rate_card_save_does_not_overwrite_preference():
    # rating='save' now maps to preference='up' (B1.5 lifecycle semantics).
    # The ON CONFLICT SQL still uses EXCLUDED IS DISTINCT FROM guards so that
    # a later 'open' (preference='none') cannot clobber an existing 'up'.
    calls = _capture_rate_calls("save")
    assert len(calls) >= 2, "expected pulse_ratings + paper_user_state inserts"
    state_call = calls[1]
    sql = state_call.args[0]
    preference_param = state_call.args[4]
    assert preference_param == "up"
    assert "EXCLUDED.preference IS DISTINCT FROM 'none'" in sql


def test_rate_card_open_does_not_set_thumbs_up():
    # rating='open' is a navigation event, not a preference signal.
    calls = _capture_rate_calls("open")
    assert len(calls) >= 2
    preference_param = calls[1].args[4]
    assert preference_param == "none"


def test_rate_card_up_records_thumbs_up_preference():
    calls = _capture_rate_calls("up")
    assert calls[1].args[4] == "up"


def test_rate_card_down_records_thumbs_down_preference():
    calls = _capture_rate_calls("down")
    assert calls[1].args[4] == "down"


def test_rate_card_dismiss_records_thumbs_down_preference():
    calls = _capture_rate_calls("dismiss")
    assert calls[1].args[4] == "down"


# ---------------------------------------------------------------------------
# B3.2: rate_card lifecycle semantics (WS8)
# ---------------------------------------------------------------------------


def test_rate_card_save_writes_saved_starred_pref_up():
    """rating='save' must bind starred=True, saved=True, preference='up'.

    The paper_user_state INSERT call (calls[1]) must carry the correct
    positional args so that on first-visit the row is written correctly and
    on conflict the EXCLUDED IS DISTINCT FROM guards promote each field.

    Positional layout of the paper_user_state execute call:
      args[0] = SQL, args[1] = paper_id, args[2] = user_id,
      args[3] = starred, args[4] = preference, args[5] = saved, args[6] = dismissed
    """
    calls = _capture_rate_calls("save")
    assert len(calls) >= 2, "expected pulse_ratings + paper_user_state inserts"
    state_args = calls[1].args
    assert state_args[3] is True, f"starred must be True for 'save', got {state_args[3]!r}"
    assert state_args[4] == "up", f"preference must be 'up' for 'save', got {state_args[4]!r}"
    assert state_args[5] is True, f"saved must be True for 'save', got {state_args[5]!r}"


def test_rate_card_dismiss_writes_dismissed_pref_down_preserves_saved():
    """rating='dismiss' must bind dismissed=True, preference='down', saved=False.

    The saved=False bound value triggers the EXCLUDED IS DISTINCT FROM FALSE
    guard in the ON CONFLICT clause — when the existing row has saved=True the
    SQL preserves it.  This test verifies that the bound parameter for saved is
    False (the preserve-safe default) so the SQL's CASE WHEN guard can do its job.

    Positional layout: args[3]=starred, args[4]=preference, args[5]=saved, args[6]=dismissed
    """
    calls = _capture_rate_calls("dismiss")
    assert len(calls) >= 2, "expected pulse_ratings + paper_user_state inserts"
    state_args = calls[1].args
    sql: str = state_args[0]
    assert state_args[4] == "down", (
        f"preference must be 'down' for 'dismiss', got {state_args[4]!r}"
    )
    assert state_args[6] is True, f"dismissed must be True for 'dismiss', got {state_args[6]!r}"
    # saved bound as False so the ON CONFLICT CASE WHEN preserves an existing True
    assert state_args[5] is False, (
        f"saved must be False (preserve-safe) for 'dismiss', got {state_args[5]!r}"
    )
    # Confirm the SQL has the IS DISTINCT FROM guard that makes preservation work
    assert "EXCLUDED.saved IS DISTINCT FROM FALSE" in sql, (
        f"Expected EXCLUDED.saved IS DISTINCT FROM FALSE in SQL:\n{sql!r}"
    )


def test_rate_card_open_is_noop_on_state():
    """rating='open' is a navigation event — no destructive state mutation.

    The paper_user_state INSERT must bind all fields to their neutral defaults:
    starred=False, preference='none', saved=False, dismissed=False.
    Combined with the ON CONFLICT EXCLUDED IS DISTINCT FROM guards this means
    no existing field value can be overwritten by an 'open' event.
    """
    calls = _capture_rate_calls("open")
    assert len(calls) >= 2, "expected pulse_ratings + paper_user_state inserts"
    state_args = calls[1].args
    sql: str = state_args[0]
    assert state_args[3] is False, f"starred must be False for 'open' (noop), got {state_args[3]!r}"
    assert state_args[4] == "none", (
        f"preference must be 'none' for 'open' (noop), got {state_args[4]!r}"
    )
    assert state_args[5] is False, f"saved must be False for 'open' (noop), got {state_args[5]!r}"
    assert state_args[6] is False, (
        f"dismissed must be False for 'open' (noop), got {state_args[6]!r}"
    )
    # Confirm all four IS DISTINCT FROM guards are present so no field is clobbered
    assert "EXCLUDED.preference IS DISTINCT FROM 'none'" in sql, (
        "Missing preference no-clobber guard in SQL"
    )
    assert "EXCLUDED.starred IS DISTINCT FROM FALSE" in sql, (
        "Missing starred no-clobber guard in SQL"
    )
    assert "EXCLUDED.saved IS DISTINCT FROM FALSE" in sql, "Missing saved no-clobber guard in SQL"
    assert "EXCLUDED.dismissed IS DISTINCT FROM FALSE" in sql, (
        "Missing dismissed no-clobber guard in SQL"
    )
