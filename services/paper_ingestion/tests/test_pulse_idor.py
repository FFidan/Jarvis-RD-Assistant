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

    # conn.fetchval is called multiple times: first for the deck-membership guard,
    # then once more inside _upsert_recommendation_feedback for the topic_id lookup.
    # Use await_args_list[0] to pin to the first (deck-guard) call.
    assert conn.fetchval.await_count >= 1, "conn.fetchval was never awaited"
    call_args = conn.fetchval.await_args_list[0]
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
    """When user_id=7 is resolved, $2 in the deck guard must be 7.

    rate_card calls current_user_id_or_none(request) directly (not via Depends),
    so we patch it at the pulse router module level to inject the desired user_id.
    """
    from unittest.mock import AsyncMock, patch

    tc, pool, conn, app = _make_client(user_id_override=7)

    conn.fetchval.return_value = 1
    conn.execute.return_value = "INSERT 0 1"

    with patch(
        "paper_ingestion.routers.pulse.current_user_id_or_none",
        new=AsyncMock(return_value=7),
    ):
        try:
            resp = tc.post("/api/pulse/rate", json={"paper_id": 99, "rating": "down"})
        finally:
            app.dependency_overrides.clear()
            from paper_ingestion.deps import limiter

            limiter.enabled = True

    assert resp.status_code == 200

    # Use await_args_list[0] — the first fetchval is the deck-guard; subsequent
    # ones are topic_id lookups inside _upsert_recommendation_feedback.
    call_args = conn.fetchval.await_args_list[0]
    assert call_args is not None
    params = call_args.args[1:]
    assert len(params) == 2
    assert params[0] == 99  # paper_id
    assert params[1] == 7  # user_id bound to $2


# ---------------------------------------------------------------------------
# Test 3: rate_card per-rating helper dispatch (C3 — post-B4 redesign)
#
# rate_card now routes to helper functions in routers/_paper_helpers.py.
# All tests patch at the routers.pulse module path.
# ---------------------------------------------------------------------------


from unittest.mock import AsyncMock, patch  # noqa: E402 — grouped with other imports above


def _rate(rating: str, paper_id: int = 1, user_id=None):
    """Issue a POST /api/pulse/rate and return (resp, app) for cleanup."""
    tc, pool, conn, app = _make_client(user_id_override=user_id)
    conn.fetchval.return_value = 1  # deck membership guard passes
    try:
        resp = tc.post("/api/pulse/rate", json={"paper_id": paper_id, "rating": rating})
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True
    return resp


def test_rate_open_writes_nothing():
    """rating='open' returns 200; no helper writes any DB row."""
    with (
        patch(
            "paper_ingestion.routers.pulse._upsert_recommendation_feedback", new_callable=AsyncMock
        ) as mock_fb,
        patch(
            "paper_ingestion.routers.pulse._upsert_state_and_starred", new_callable=AsyncMock
        ) as mock_state,
        patch("paper_ingestion.routers.pulse._trash_paper", new_callable=AsyncMock) as mock_trash,
    ):
        resp = _rate("open")

    assert resp.status_code == 200
    mock_fb.assert_not_called()
    mock_state.assert_not_called()
    mock_trash.assert_not_called()


def test_rate_save_writes_state_only():
    """rating='save' calls _upsert_state_and_starred(state='to_read') and NOT _upsert_recommendation_feedback."""
    with (
        patch(
            "paper_ingestion.routers.pulse._upsert_state_and_starred", new_callable=AsyncMock
        ) as mock_state,
        patch(
            "paper_ingestion.routers.pulse._upsert_recommendation_feedback", new_callable=AsyncMock
        ) as mock_fb,
    ):
        resp = _rate("save")

    assert resp.status_code == 200
    mock_state.assert_awaited_once()
    assert mock_state.await_args.kwargs.get("state") == "to_read"
    mock_fb.assert_not_called()


def test_rate_up_writes_recommendation_feedback_positive_pulse_thumbs():
    """rating='up' calls _upsert_recommendation_feedback(signal='positive', source='pulse_thumbs')."""
    with patch(
        "paper_ingestion.routers.pulse._upsert_recommendation_feedback", new_callable=AsyncMock
    ) as mock_fb:
        resp = _rate("up", paper_id=55)

    assert resp.status_code == 200
    mock_fb.assert_awaited_once()
    _conn, paper_id_arg, _uid, signal_arg, source_arg = mock_fb.await_args.args
    assert paper_id_arg == 55
    assert signal_arg == "positive"
    assert source_arg == "pulse_thumbs"


def test_rate_down_writes_recommendation_feedback_negative_pulse_thumbs():
    """rating='down' calls _upsert_recommendation_feedback(signal='negative', source='pulse_thumbs')."""
    with patch(
        "paper_ingestion.routers.pulse._upsert_recommendation_feedback", new_callable=AsyncMock
    ) as mock_fb:
        resp = _rate("down", paper_id=77)

    assert resp.status_code == 200
    mock_fb.assert_awaited_once()
    _conn, paper_id_arg, _uid, signal_arg, source_arg = mock_fb.await_args.args
    assert paper_id_arg == 77
    assert signal_arg == "negative"
    assert source_arg == "pulse_thumbs"


def test_rate_dismiss_writes_state_trash_and_recommendation_feedback_negative_dismiss_combined():
    """rating='dismiss' calls _trash_paper AND _upsert_recommendation_feedback(signal='negative', source='dismiss_combined')."""
    with (
        patch("paper_ingestion.routers.pulse._trash_paper", new_callable=AsyncMock) as mock_trash,
        patch(
            "paper_ingestion.routers.pulse._upsert_recommendation_feedback", new_callable=AsyncMock
        ) as mock_fb,
    ):
        resp = _rate("dismiss", paper_id=99)

    assert resp.status_code == 200
    mock_trash.assert_awaited_once()
    _conn, paper_id_arg, _uid = mock_trash.await_args.args
    assert paper_id_arg == 99

    mock_fb.assert_awaited_once()
    _conn, fb_paper_id, _uid, signal_arg, source_arg = mock_fb.await_args.args
    assert fb_paper_id == 99
    assert signal_arg == "negative"
    assert source_arg == "dismiss_combined"


def test_rate_card_membership_guard_returns_404_when_paper_not_in_deck():
    """POST /rate returns 404 when the paper is NOT in the requesting user's pulse deck."""
    tc, pool, conn, app = _make_client(user_id_override=42)
    conn.fetchval.return_value = None  # guard: paper not in deck
    try:
        resp = tc.post("/api/pulse/rate", json={"paper_id": 999, "rating": "up"})
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 404, (
        f"Expected 404 when paper not in deck, got {resp.status_code}: {resp.text}"
    )


def test_rate_card_open_returns_ok_status():
    """rating='open' returns HTTP 200 with status='ok' (logging-only path)."""
    tc, pool, conn, app = _make_client(user_id_override=None)
    conn.fetchval.return_value = 1
    try:
        resp = tc.post("/api/pulse/rate", json={"paper_id": 5, "rating": "open"})
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("status") == "ok", f"Expected status='ok' for 'open' path, got {data!r}"
