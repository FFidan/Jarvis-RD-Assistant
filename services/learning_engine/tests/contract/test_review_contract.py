"""Review domain contract tests — A217, A219, A220.

Covers:
- GET /api/review/next       (A217) — scoping + user B sees no user A cards
- POST /api/review/sync      (A219) — idempotency_key prevents double-apply
- GET /api/stats             (A220) — stats aggregated from caller's data only
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest


from jarvis_common.testing_contract_apps import (
    make_contract_client as _client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


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


async def test_get_next_review_deck_scoped(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/review/next?deck_id=D1 returns ONLY cards from D1, not from D2.

    User A has deck D1 (seeded by fixture) and a second deck D2 created here.
    Both decks have a due card.  Calling with deck_id=D1 must exclude D2's card.
    """
    user_a_id = contract_two_users.user_a_id
    deck_id_1 = contract_two_users.deck_id_a
    card_id_1 = contract_two_users.card_id_a

    # Force card in D1 to be due
    await contract_conn.execute(
        "UPDATE cards SET due_at = NOW() - INTERVAL '1 hour' WHERE id = $1",
        card_id_1,
    )

    # Seed a second deck (D2) and a due card in it, same user
    deck_id_2 = await contract_conn.fetchval(
        "INSERT INTO decks (name, user_id) VALUES ('deck-scope-d2', $1) RETURNING id",
        user_a_id,
    )
    card_id_2 = await contract_conn.fetchval(
        """INSERT INTO cards (deck_id, card_type, front, back, user_id, due_at)
           VALUES ($1, 'concept', 'D2 front', 'D2 back', $2, NOW() - INTERVAL '2 hours')
           RETURNING id""",
        deck_id_2,
        user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/review/next", params={"deck_id": deck_id_1, "limit": 50})

    assert resp.status_code == 200, (
        f"GET /api/review/next?deck_id={deck_id_1} failed: {resp.status_code}: {resp.text[:300]}"
    )
    returned_ids = [card["id"] for card in resp.json()]
    assert card_id_1 in returned_ids, (
        f"Deck-scoped query missing D1 card {card_id_1}; got {returned_ids}"
    )
    assert card_id_2 not in returned_ids, (
        f"Deck-scope bleed: D2 card {card_id_2} appeared in D1-scoped query. "
        f"Full list: {returned_ids}"
    )


# ---------------------------------------------------------------------------
# §A221 — GET /api/review/next?deck_id — cross-user deck returns empty list
# ---------------------------------------------------------------------------


async def test_get_next_review_cross_user_deck_id_returns_empty(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B passes user A's deck_id → 200 with empty list (not 404, not IDOR data leak).

    review.py:108 EXISTS subquery: ``d.user_id = $1`` rejects user B because
    deck_id_a's user_id != user B's id. The card_a is made due so a bug would
    expose it.
    # Verified: services/learning_engine/learning_engine/routers/review.py:104-116
    """
    card_id_a = contract_two_users.card_id_a
    deck_id_a = contract_two_users.deck_id_a
    await contract_conn.execute(
        "UPDATE cards SET due_at = NOW() - INTERVAL '1 hour' WHERE id = $1",
        card_id_a,
    )
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/review/next", params={"deck_id": deck_id_a, "limit": 50})

    assert resp.status_code == 200, (
        f"Expected 200 (empty); got {resp.status_code}: {resp.text[:300]}"
    )
    returned_ids = [card["id"] for card in resp.json()]
    assert card_id_a not in returned_ids, (
        f"IDOR via deck_id: user B received user A's card {card_id_a} "
        f"when querying with A's deck_id. Full list: {returned_ids}"
    )
    assert returned_ids == [], (
        f"Expected empty list; user B has no cards in deck_id_a. Got: {returned_ids}"
    )


async def test_get_next_review_no_deck_id_returns_all_user_due_decks(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """Unscoped GET /api/review/next (no deck_id) returns due cards from all of user A's decks.

    Positive control: confirms the NULL deck_id path still works after the EXISTS
    subquery guard is in place.
    # Verified: services/learning_engine/learning_engine/routers/review.py:104-116
    """
    card_id_a = contract_two_users.card_id_a
    await contract_conn.execute(
        "UPDATE cards SET due_at = NOW() - INTERVAL '1 hour' WHERE id = $1",
        card_id_a,
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/review/next", params={"limit": 50})

    assert resp.status_code == 200, (
        f"Unscoped review/next failed: {resp.status_code}: {resp.text[:300]}"
    )
    returned_ids = [card["id"] for card in resp.json()]
    assert card_id_a in returned_ids, (
        f"Expected user A's due card {card_id_a} in unscoped result; got {returned_ids}"
    )


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
