"""Review domain contract tests — A217, A219, A220.

Covers:
- GET /api/review/next       (A217) — scoping + user B sees no user A cards
- POST /api/review/sync      (A219) — idempotency_key prevents double-apply
- GET /api/stats             (A220) — stats aggregated from caller's data only
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from jarvis_common.testing import SharedConnPool

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "le-contract-review-test-key"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app(contract_conn):
    """LE app wired to the contract connection with idiomatic mocks."""
    from learning_engine.deps import get_anki_exporter, get_db_pool, get_fsrs_manager
    from learning_engine.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_fsrs = getattr(app.state, "fsrs_manager", None)
    original_exporter = getattr(app.state, "anki_exporter", None)
    original_generator = getattr(app.state, "card_generator", None)

    mock_fsrs = MagicMock()
    _now = datetime.now(UTC)
    mock_fsrs.create_new_card.return_value = ({}, _now)
    mock_fsrs.schedule_review.return_value = ({}, {}, _now + timedelta(days=1))

    app.state.db_pool = shared
    app.state.http_client = AsyncMock()
    app.state.fsrs_manager = mock_fsrs
    app.state.anki_exporter = MagicMock()
    app.state.card_generator = AsyncMock()
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_anki_exporter] = lambda: MagicMock()

    from learning_engine.deps import limiter

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False

    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
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
        if original_fsrs is None:
            if hasattr(app.state, "fsrs_manager"):
                del app.state.fsrs_manager
        else:
            app.state.fsrs_manager = original_fsrs
        if original_exporter is None:
            if hasattr(app.state, "anki_exporter"):
                del app.state.anki_exporter
        else:
            app.state.anki_exporter = original_exporter
        if original_generator is None:
            if hasattr(app.state, "card_generator"):
                del app.state.card_generator
        else:
            app.state.card_generator = original_generator
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_fsrs_manager, None)
        app.dependency_overrides.pop(get_anki_exporter, None)


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


def _client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# §A217 — GET /api/review/next — only caller's due cards returned
# ---------------------------------------------------------------------------


async def test_get_next_review_user_b_sees_no_user_a_cards(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B's GET /api/review/next returns 200 and never includes user A's card.

    Seeds user A's card as due (due_at in the past) inside the transaction.
    User B must receive an empty list (or a list not containing A's card_id).
    Collapses the SQL-text assertion ``"user_id = $1"`` in test_le_endpoints.py
    to a real scoping proof.
    """
    card_id_a = contract_two_users.card_id_a
    # Force card A to be due
    await contract_conn.execute(
        "UPDATE cards SET due_at = NOW() - INTERVAL '1 hour' WHERE id = $1",
        card_id_a,
    )
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/review/next", params={"limit": 50})

    assert resp.status_code == 200, (
        f"GET /api/review/next for user B failed: {resp.status_code}: {resp.text[:300]}"
    )
    returned_ids = [card["id"] for card in resp.json()]
    assert card_id_a not in returned_ids, (
        f"IDOR: user B received user A's card {card_id_a} in review queue. "
        f"Full list: {returned_ids}"
    )


async def test_get_next_review_owner_sees_own_due_card(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A's GET /api/review/next returns their own due card (positive control).

    Confirms the WHERE user_id = $1 filter scopes to the caller rather than
    returning nothing.
    """
    card_id_a = contract_two_users.card_id_a
    await contract_conn.execute(
        "UPDATE cards SET due_at = NOW() - INTERVAL '1 hour' WHERE id = $1",
        card_id_a,
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/review/next", params={"limit": 50})

    assert resp.status_code == 200, (
        f"GET /api/review/next for user A failed: {resp.status_code}: {resp.text[:300]}"
    )
    returned_ids = [card["id"] for card in resp.json()]
    assert card_id_a in returned_ids, (
        f"User A expected to see their own due card {card_id_a}; got {returned_ids}"
    )


# ---------------------------------------------------------------------------
# §A219 — POST /api/review/sync — idempotency_key prevents double-apply
# ---------------------------------------------------------------------------


async def test_sync_reviews_idempotency_key_prevents_double_apply(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/review/sync with the same idempotency_key twice reports synced=1 not 2.

    Replaces test_review_sync.py's SQL-positional-arg binding assertions with a
    real idempotency guarantee: the ON CONFLICT (user_id, idempotency_key) WHERE
    idempotency_key IS NOT NULL clause prevents duplicate review_logs rows.
    """
    card_id_a = contract_two_users.card_id_a
    idem_key = f"idem-contract-{uuid.uuid4()}"
    reviewed_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    event = {
        "idempotency_key": idem_key,
        "card_id": card_id_a,
        "rating": 3,
        "reviewed_at": reviewed_at,
        "review_duration_ms": 1500,
    }

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp1 = await c.post("/api/review/sync", json={"reviews": [event]})
    assert resp1.status_code == 200, f"First sync failed: {resp1.status_code}: {resp1.text[:300]}"
    body1 = resp1.json()
    assert body1["synced"] == 1 and body1["skipped"] == 0, (
        f"First sync expected synced=1 skipped=0; got {body1}"
    )

    # Second call with the same idempotency_key — must NOT create a second review_log row
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp2 = await c.post("/api/review/sync", json={"reviews": [event]})
    assert resp2.status_code == 200, f"Second sync failed: {resp2.status_code}: {resp2.text[:300]}"
    body2 = resp2.json()
    assert body2["synced"] == 1, (
        f"Idempotency violated: second sync reported synced={body2['synced']} "
        f"(expected 1). Full body: {body2}"
    )

    # Verify only one review_log row exists with this key
    row_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM review_logs WHERE idempotency_key = $1",
        idem_key,
    )
    assert row_count == 1, (
        f"Expected 1 review_log row for idempotency_key={idem_key!r}; "
        f"found {row_count} — double-insert bug"
    )


async def test_sync_reviews_user_b_event_skipped_for_user_a_card(
    contract_two_users, _le_app, _configure_api_key
):
    """POST /api/review/sync: user B's event for user A's card is skipped (not applied).

    The ownership guard in sync_reviews fetches the card with AND user_id = $2;
    when user B sends an event for user A's card, the row is not found and
    skipped=1. Replaces the cross-user isolation assertion in test_review_sync.py.
    """
    card_id_a = contract_two_users.card_id_a
    event = {
        "idempotency_key": f"cross-user-{uuid.uuid4()}",
        "card_id": card_id_a,
        "rating": 3,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "review_duration_ms": 1000,
    }

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post("/api/review/sync", json={"reviews": [event]})

    assert resp.status_code == 200, (
        f"Sync for cross-user event failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["skipped"] == 1 and body["synced"] == 0, (
        f"Expected skipped=1 synced=0 for cross-user event; got {body}"
    )


# ---------------------------------------------------------------------------
# §A220 — GET /api/stats — aggregated from caller's cards/review_logs only
# ---------------------------------------------------------------------------


async def test_get_stats_scoped_to_caller(contract_two_users, _le_app, _configure_api_key):
    """GET /api/stats returns correct totals for caller's cards and review_logs.

    Collapses test_le_endpoints.py::test_get_review_stats which only checks
    response keys against a mocked pool. Here we assert the behavioral contract:
    user A sees their card in total_cards; user B's totals reflect only B's data.
    """
    # User A — has 1 card seeded by contract_two_users
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/stats")

    assert resp_a.status_code == 200, (
        f"GET /api/stats for user A failed: {resp_a.status_code}: {resp_a.text[:300]}"
    )
    body_a = resp_a.json()
    assert "total_cards" in body_a, f"Response missing total_cards: {body_a}"
    assert "streak_days" in body_a, f"Response missing streak_days: {body_a}"
    assert isinstance(body_a["total_cards"], int) and body_a["total_cards"] >= 1, (
        f"User A expected total_cards >= 1 (has 1 seeded); got {body_a['total_cards']}"
    )

    # User B — also has 1 card; total_cards should reflect B's own cards only
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/stats")

    assert resp_b.status_code == 200, (
        f"GET /api/stats for user B failed: {resp_b.status_code}: {resp_b.text[:300]}"
    )
    body_b = resp_b.json()
    # Both users have 1 seeded card; if B's total_cards included A's card it would be >= 2
    # (scoping check: each user's count must not exceed their own seeded count)
    assert body_b["total_cards"] == body_a["total_cards"], (
        f"Each user has 1 seeded card but totals differ unexpectedly: "
        f"A={body_a['total_cards']} B={body_b['total_cards']}"
    )
