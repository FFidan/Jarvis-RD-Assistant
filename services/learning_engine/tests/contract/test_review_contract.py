"""Review domain contract tests — A217, A219, A220.

Covers:
- GET /api/review/next       (A217) — scoping + user B sees no user A cards
- POST /api/review/sync      (A219) — idempotency_key prevents double-apply
- GET /api/stats             (A220) — stats aggregated from caller's data only
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

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

    # Second call with the same idempotency_key — must NOT create a second review_log row.
    # The key is now in ``applied``; the event hits the idempotency fast-path and is
    # counted as ``already_synced`` (not ``synced``) to distinguish replays from new writes.
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp2 = await c.post("/api/review/sync", json={"reviews": [event]})
    assert resp2.status_code == 200, f"Second sync failed: {resp2.status_code}: {resp2.text[:300]}"
    body2 = resp2.json()
    assert body2["synced"] == 0 and body2["already_synced"] == 1, (
        f"Idempotency fast-path should report synced=0 already_synced=1; got {body2}"
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
# §A219-update-guard — POST /api/review/sync — UPDATE WHERE user_id guard
# ---------------------------------------------------------------------------


async def test_sync_reviews_update_does_not_mutate_cross_user_card(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B's sync cannot mutate user A's card via the UPDATE path.

    Proof strategy:
    1. Capture user A's card fsrs_state before any review.
    2. User A submits a successful sync (establishes idempotency_key as applied).
    3. Capture fsrs_state after user A's sync.
    4. User B submits a sync for the same card_id with a *new* idempotency_key.
       - The SELECT fetchrow (AND user_id = $2) skips the event (skipped=1).
       - The UPDATE (AND user_id = $4) would also have rejected it if reached.
    5. Assert the card's fsrs_state is identical to step 3 (B's event made no write).

    The UPDATE guard (AND user_id = $4) added at review.py:257 is the final
    defense-in-depth layer ensuring that even if the fetchrow guard were absent,
    the UPDATE cannot silently advance another user's FSRS state.
    """
    card_id_a = contract_two_users.card_id_a

    fsrs_before = await contract_conn.fetchval(
        "SELECT fsrs_state FROM cards WHERE id = $1",
        card_id_a,
    )

    # User A syncs first to establish a valid review record.
    idem_key_a = f"update-guard-a-{uuid.uuid4()}"
    event_a = {
        "idempotency_key": idem_key_a,
        "card_id": card_id_a,
        "rating": 3,
        "reviewed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "review_duration_ms": 1200,
    }
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.post("/api/review/sync", json={"reviews": [event_a]})
    assert resp_a.status_code == 200, (
        f"User A sync failed: {resp_a.status_code}: {resp_a.text[:300]}"
    )
    assert resp_a.json()["synced"] == 1

    fsrs_after_a = await contract_conn.fetchval(
        "SELECT fsrs_state FROM cards WHERE id = $1",
        card_id_a,
    )
    # Sanity check: user A's sync must have updated the FSRS state.
    assert fsrs_after_a != fsrs_before, (
        f"User A's sync did not advance fsrs_state; test setup incomplete. "
        f"Before: {fsrs_before!r}  After: {fsrs_after_a!r}"
    )

    # User B attempts to sync the same card with a fresh idempotency_key.
    # The fetchrow guard (WHERE id=$1 AND user_id=$2) catches this first;
    # the UPDATE guard (AND user_id=$4) is the backstop if the fetchrow were absent.
    event_b = {
        "idempotency_key": f"update-guard-b-{uuid.uuid4()}",
        "card_id": card_id_a,
        "rating": 4,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "review_duration_ms": 800,
    }
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.post("/api/review/sync", json={"reviews": [event_b]})
    assert resp_b.status_code == 200, (
        f"User B sync failed: {resp_b.status_code}: {resp_b.text[:300]}"
    )
    body_b = resp_b.json()
    assert body_b["skipped"] == 1 and body_b["synced"] == 0, (
        f"Expected skipped=1 synced=0 for cross-user event; got {body_b}"
    )

    # The card's fsrs_state must be exactly what user A left it — B wrote nothing.
    fsrs_after_b = await contract_conn.fetchval(
        "SELECT fsrs_state FROM cards WHERE id = $1",
        card_id_a,
    )
    assert fsrs_after_b == fsrs_after_a, (
        f"UPDATE guard FAILED: user B's sync mutated card {card_id_a}. "
        f"fsrs_state after A={fsrs_after_a!r}  after B={fsrs_after_b!r}"
    )


# ---------------------------------------------------------------------------
# §A218 — POST /api/review/{card_id} — owned-card UPDATE scoped by user_id
# ---------------------------------------------------------------------------


async def test_submit_review_owned_card_advances_and_persists_state(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/review/{card_id} on the caller's own card advances fsrs_state and
    persists it. The cards UPDATE is scoped by user_id (defense-in-depth); this
    confirms the predicate still matches the owner's row (UPDATE affects 1 row).
    """
    card_id_a = contract_two_users.card_id_a
    before = await contract_conn.fetchval("SELECT fsrs_state FROM cards WHERE id = $1", card_id_a)
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/review/{card_id_a}", json={"rating": 3, "review_duration_ms": 500}
        )
    assert resp.status_code == 200, f"submit_review failed: {resp.status_code}: {resp.text[:300]}"
    after = await contract_conn.fetchval("SELECT fsrs_state FROM cards WHERE id = $1", card_id_a)
    assert after != before, (
        f"owned-card review must advance and persist fsrs_state; before={before!r} after={after!r}"
    )


async def test_stale_card_review_paths_are_terminal_without_mutation(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
):
    """Direct and offline review reject retained earlier-generation cards."""
    card_id = contract_two_users.card_id_a
    paper_id = contract_two_users.paper_id_a
    user_id = contract_two_users.user_a_id
    await contract_conn.execute(
        """
        UPDATE cards
        SET due_at = NOW() - INTERVAL '1 hour',
            content_generation = (
                SELECT content_generation FROM papers WHERE id = $2
            )
        WHERE id = $1
        """,
        card_id,
        paper_id,
    )
    before = await contract_conn.fetchrow(
        "SELECT fsrs_state, due_at, updated_at FROM cards WHERE id = $1",
        card_id,
    )
    daily_before = await contract_conn.fetchval(
        """
        SELECT COALESCE(cards_reviewed, 0)
        FROM daily_log
        WHERE user_id = $1 AND log_date = CURRENT_DATE
        """,
        user_id,
    )
    await contract_conn.execute(
        "UPDATE papers SET content_generation = content_generation + 1 WHERE id = $1",
        paper_id,
    )
    idem_key = f"stale-card-{uuid.uuid4()}"
    event = {
        "idempotency_key": idem_key,
        "card_id": card_id,
        "rating": 3,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "review_duration_ms": 250,
    }

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        direct = await c.post(
            f"/api/review/{card_id}",
            json={"rating": 3, "review_duration_ms": 250},
        )
        offline = await c.post("/api/review/sync", json={"reviews": [event]})

    assert direct.status_code == 409, direct.text[:300]
    assert offline.status_code == 200, offline.text[:300]
    assert offline.json() == {"synced": 0, "skipped": 1, "already_synced": 0}
    after = await contract_conn.fetchrow(
        "SELECT fsrs_state, due_at, updated_at FROM cards WHERE id = $1",
        card_id,
    )
    assert dict(after) == dict(before)
    assert (
        await contract_conn.fetchval(
            "SELECT COUNT(*) FROM review_logs WHERE idempotency_key = $1",
            idem_key,
        )
        == 0
    )
    daily_after = await contract_conn.fetchval(
        """
        SELECT COALESCE(cards_reviewed, 0)
        FROM daily_log
        WHERE user_id = $1 AND log_date = CURRENT_DATE
        """,
        user_id,
    )
    assert (daily_after or 0) == (daily_before or 0)


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


async def test_get_stats_scoped_to_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/stats returns correct totals for caller's cards and review_logs.

    Collapses test_le_endpoints.py::test_get_review_stats which only checks
    response keys against a mocked pool. Here we assert the behavioral contract:
    user A sees their card in total_cards; user B's totals reflect only B's data.

    IDNF proof: a NULL-user_id card must NOT be counted in user A's total_cards.
    The card_stats CTE uses ``user_id IS NOT DISTINCT FROM $1`` — with $1 = A's
    integer id, a NULL-user_id row does NOT match, so total_cards is unaffected.
    """
    deck_id_a = contract_two_users.deck_id_a
    user_a_id = contract_two_users.user_a_id

    # Seed a card with user_id = NULL (the IDNF sentinel).
    null_card_id = await contract_conn.fetchval(
        "INSERT INTO cards (deck_id, card_type, front, back, user_id)"
        " VALUES ($1, 'concept', 'null-user front', 'null-user back', NULL)"
        " RETURNING id",
        deck_id_a,
    )

    # User A — has 1 card seeded by contract_two_users (plus the NULL-user card above)
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
    # The NULL-user card must NOT inflate user A's count (IDNF: NULL IS NOT DISTINCT FROM
    # integer = false, so it is correctly excluded).
    null_card_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM cards WHERE id = $1 AND user_id IS NULL",
        null_card_id,
    )
    assert null_card_count == 1, "NULL-user card not seeded correctly — test setup error"
    # If card_stats used strict `=` instead of IDNF, a NULL-user card owned by the same
    # deck would still not match (= with NULL is always false), but a NULL $1 would include
    # everything. Here user_a_id IS an integer, so we confirm total_cards equals exactly
    # the count of user A's own cards (not inflated by the NULL-user row).
    own_card_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM cards WHERE user_id = $1",
        user_a_id,
    )
    assert body_a["total_cards"] == own_card_count, (
        f"total_cards={body_a['total_cards']} but user A owns {own_card_count} cards; "
        f"NULL-user card (id={null_card_id}) must NOT be counted (IDNF semantic)"
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


async def test_sync_reviews_releases_connection_between_batches(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/review/sync with ≥100 events acquires the pool connection > 1 time.

    Proves the N=50 batching refactor (LE-D5-05): one acquire for the pre-flight
    idempotency-key lookup, then one acquire per 50-event chunk.  Also confirms
    idempotency: re-sending the same body reports synced=0.
    """
    card_id_a = contract_two_users.card_id_a
    reviewed_at = (datetime.now(UTC) - timedelta(hours=3)).isoformat()

    # Build 100 distinct events — all for user A's card, each with a unique key.
    n_events = 100
    events = [
        {
            "idempotency_key": f"batch-proof-{uuid.uuid4()}",
            "card_id": card_id_a,
            "rating": 3,
            "reviewed_at": reviewed_at,
            "review_duration_ms": 800,
        }
        for _ in range(n_events)
    ]

    # Instrument db_pool.acquire to count invocations.
    pool = _le_app.state.db_pool
    original_acquire = pool.acquire
    acquire_count = 0

    def counting_acquire():
        nonlocal acquire_count
        acquire_count += 1
        return original_acquire()

    pool.acquire = counting_acquire

    try:
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/review/sync", json={"reviews": events})
    finally:
        pool.acquire = original_acquire  # always restore

    assert resp.status_code == 200, f"Sync failed: {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    # All 100 events are new — expect synced=100, skipped=0.
    assert body["synced"] == n_events and body["skipped"] == 0, (
        f"Expected synced={n_events} skipped=0; got {body}"
    )
    # Pre-flight + ceil(100/50)=2 chunk acquires → 3 total > 1.
    assert acquire_count > 1, (
        f"Expected > 1 pool.acquire() calls (batching proof); got {acquire_count}. "
        "sync_reviews may be holding one connection for the whole loop."
    )

    # Re-send the same body — idempotency must hold: synced=0.
    acquire_count = 0
    pool.acquire = counting_acquire
    try:
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp2 = await c.post("/api/review/sync", json={"reviews": events})
    finally:
        pool.acquire = original_acquire

    assert resp2.status_code == 200, f"Re-send failed: {resp2.status_code}: {resp2.text[:300]}"
    body2 = resp2.json()
    # All 100 events are already applied — expect synced=0, already_synced=100.
    assert body2["synced"] == 0 and body2["already_synced"] == n_events and body2["skipped"] == 0, (
        f"Idempotency broken on re-send: expected synced=0 already_synced={n_events} skipped=0; got {body2}"
    )
    # Confirm no double-insert: total row count for these keys must still be n_events.
    idem_keys = [e["idempotency_key"] for e in events]
    row_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM review_logs WHERE idempotency_key = ANY($1::text[])",
        idem_keys,
    )
    assert row_count == n_events, (
        f"Double-insert detected: expected {n_events} review_log rows; found {row_count}"
    )


# ---------------------------------------------------------------------------
# §A219-race — POST /api/review/sync — concurrent INSERT RETURNING None branch
# ---------------------------------------------------------------------------


async def test_sync_reviews_concurrent_on_conflict_returns_already_synced(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/review/sync counts already_synced=1 when RETURNING is None due to ON CONFLICT.

    Exercises the ``inserted_log_id is None`` branch (review.py:249-254) that handles
    the race between two concurrent requests sharing the same (user_id, idempotency_key).
    The second request's INSERT ... ON CONFLICT DO NOTHING RETURNING id yields NULL
    because the first request's row already exists, but it was NOT in the pre-flight
    applied-set (the race window between the pre-flight SELECT and the per-event INSERT).

    Strategy: bypass true concurrency by patching pool.acquire so that the chunk-phase
    connection's fetchval returns None for the INSERT call, simulating the lost race.
    The pre-flight acquire (first call) is left unpatched so the key is NOT in ``applied``.
    """
    card_id_a = contract_two_users.card_id_a
    idem_key = f"race-cf9-{uuid.uuid4()}"
    reviewed_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    event = {
        "idempotency_key": idem_key,
        "card_id": card_id_a,
        "rating": 3,
        "reviewed_at": reviewed_at,
        "review_duration_ms": 900,
    }

    pool = _le_app.state.db_pool
    original_acquire = pool.acquire
    acquire_call_count = 0

    # PoolConnectionProxy.fetchval is read-only (asyncpg C extension), so we cannot
    # monkey-patch the instance.  Instead we patch at the module level: intercept
    # pool.acquire so that on the second call (chunk phase) we substitute the
    # pool's acquire with one whose connection is wrapped in a lightweight proxy
    # that delegates everything except fetchval for INSERT ... review_logs queries.

    class _FetchvalProxy:
        """Thin wrapper around a PoolConnectionProxy that intercepts fetchval."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def fetchval(self, query, *args, **kwargs):
            if "INSERT INTO review_logs" in query:
                return None
            return await self._conn.fetchval(query, *args, **kwargs)

        # asyncpg's transaction() returns a Transaction tied to the underlying conn;
        # proxy it so the handler's ``async with conn.transaction()`` works correctly.
        def transaction(self, *args, **kwargs):
            return self._conn.transaction(*args, **kwargs)

        async def fetchrow(self, *args, **kwargs):
            return await self._conn.fetchrow(*args, **kwargs)

        async def fetch(self, *args, **kwargs):
            return await self._conn.fetch(*args, **kwargs)

        async def execute(self, *args, **kwargs):
            return await self._conn.execute(*args, **kwargs)

    @asynccontextmanager
    async def patched_acquire():
        nonlocal acquire_call_count
        acquire_call_count += 1
        call_index = acquire_call_count

        async with original_acquire() as conn:
            if call_index == 1:
                # Pre-flight acquire — leave unpatched so the key is NOT in ``applied``.
                yield conn
            else:
                # Chunk-phase acquire — yield the proxy so fetchval returns None for INSERT.
                yield _FetchvalProxy(conn)

    pool.acquire = patched_acquire

    try:
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/review/sync", json={"reviews": [event]})
    finally:
        pool.acquire = original_acquire

    assert resp.status_code == 200, f"Sync failed: {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["synced"] == 0 and body["already_synced"] == 1 and body["skipped"] == 0, (
        f"Expected synced=0 already_synced=1 skipped=0 (RETURNING-None branch); got {body}"
    )
    # The chunk-phase connection must have been acquired (acquire_call_count >= 2).
    assert acquire_call_count >= 2, (
        f"Patch never applied — expected >= 2 pool.acquire() calls; got {acquire_call_count}. "
        "Handler may have changed structure."
    )


async def _seed_generation_race_card(pool, prefix: str) -> tuple[int, int, int, int]:
    """Commit an isolated user, paper, deck, and paper-linked card."""
    token = uuid.uuid4().hex
    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval(
                "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
                f"{prefix}-{token}@contract.test",
            )
            paper_id = await conn.fetchval(
                """
                INSERT INTO papers
                    (external_id, source_type, title, authors, url, discovered_by)
                VALUES ($1, 'arxiv', 'Generation race', ARRAY['A'], $2, $3)
                RETURNING id
                """,
                f"{prefix}-{token}",
                f"https://example.test/{token}",
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO user_library (user_id, paper_id, added_via)
                VALUES ($1, $2, 'manual_save')
                """,
                user_id,
                paper_id,
            )
            deck_id = await conn.fetchval(
                "INSERT INTO decks (name, user_id) VALUES ($1, $2) RETURNING id",
                f"{prefix}-{token}",
                user_id,
            )
            card_id = await conn.fetchval(
                """
                INSERT INTO cards
                    (deck_id, paper_id, card_type, front, back, user_id,
                     content_generation, fsrs_state, due_at)
                VALUES ($1, $2, 'concept', 'race front', 'race back', $3,
                        0, '{}'::jsonb, NOW() - INTERVAL '1 hour')
                RETURNING id
                """,
                deck_id,
                paper_id,
                user_id,
            )
    return int(user_id), int(paper_id), int(deck_id), int(card_id)


async def _reset_generation_race_card(
    pool,
    *,
    user_id: int,
    paper_id: int,
    card_id: int,
) -> None:
    """Make the card current again and remove prior review side effects."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM review_logs WHERE card_id = $1", card_id)
            await conn.execute("DELETE FROM daily_log WHERE user_id = $1", user_id)
            await conn.execute(
                """
                UPDATE cards
                SET content_generation = (
                        SELECT content_generation FROM papers WHERE id = $2
                    ),
                    fsrs_state = '{}'::jsonb,
                    due_at = NOW() - INTERVAL '1 hour',
                    updated_at = NOW()
                WHERE id = $1
                """,
                card_id,
                paper_id,
            )


async def _delete_generation_race_fixture(
    pool,
    *,
    user_id: int,
    paper_id: int,
    deck_id: int,
) -> None:
    """Remove committed concurrency-test rows."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM daily_log WHERE user_id = $1", user_id)
            await conn.execute(
                "DELETE FROM review_logs WHERE card_id IN "
                "(SELECT id FROM cards WHERE deck_id = $1)",
                deck_id,
            )
            await conn.execute("DELETE FROM cards WHERE deck_id = $1", deck_id)
            await conn.execute("DELETE FROM decks WHERE id = $1", deck_id)
            await conn.execute(
                "DELETE FROM user_library WHERE user_id = $1 AND paper_id = $2",
                user_id,
                paper_id,
            )
            await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def _replace_source(
    pool,
    paper_id: int,
    *,
    started: asyncio.Event | None = None,
    acquired: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> None:
    """Increment a source generation, optionally holding the update transaction."""
    from jarvis_common.db_helpers import lock_paper_content_generation

    async with pool.acquire() as conn:
        async with conn.transaction():
            if started is not None:
                started.set()
            await lock_paper_content_generation(conn, paper_id)
            await conn.execute(
                "UPDATE papers SET content_generation = content_generation + 1 WHERE id = $1",
                paper_id,
            )
            if acquired is not None:
                acquired.set()
            if release is not None:
                await release.wait()


class _RaceFSRSManager:
    def schedule_review(self, state, rating, review_datetime=None):
        return (
            {"scheduled": True},
            {"rating": rating},
            datetime.now(UTC) + timedelta(days=1),
        )


async def test_direct_review_and_source_replacement_serialize_in_both_orders(
    _contract_pool,
    monkeypatch,
):
    """The generation lock orders direct review and replacement without a stale write."""
    from fastapi import HTTPException

    import learning_engine.routers.review as review_module
    from learning_engine.models import ReviewRequest

    user_id, paper_id, deck_id, card_id = await _seed_generation_race_card(
        _contract_pool,
        "direct-review-race",
    )
    handler = review_module._submit_review
    try:
        action_locked = asyncio.Event()
        release_action = asyncio.Event()

        async def gated_manager(conn, user_id=None):
            action_locked.set()
            await release_action.wait()
            return _RaceFSRSManager()

        monkeypatch.setattr(review_module, "_build_fsrs_manager_from_db", gated_manager)
        action = asyncio.create_task(
            handler(
                card_id=card_id,
                body=ReviewRequest(rating=3, review_duration_ms=100),
                db_pool=_contract_pool,
                user_id=user_id,
            )
        )
        await asyncio.wait_for(action_locked.wait(), timeout=2)
        replacement_started = asyncio.Event()
        replacement_acquired = asyncio.Event()
        replacement = asyncio.create_task(
            _replace_source(
                _contract_pool,
                paper_id,
                started=replacement_started,
                acquired=replacement_acquired,
            )
        )
        await asyncio.wait_for(replacement_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert not replacement_acquired.is_set()
        release_action.set()
        result = await asyncio.wait_for(action, timeout=2)
        await asyncio.wait_for(replacement, timeout=2)
        assert result.card_id == card_id

        await _reset_generation_race_card(
            _contract_pool,
            user_id=user_id,
            paper_id=paper_id,
            card_id=card_id,
        )
        replacement_locked = asyncio.Event()
        release_replacement = asyncio.Event()
        replacement = asyncio.create_task(
            _replace_source(
                _contract_pool,
                paper_id,
                acquired=replacement_locked,
                release=release_replacement,
            )
        )
        await asyncio.wait_for(replacement_locked.wait(), timeout=2)
        forbidden_manager = AsyncMock(side_effect=AssertionError("stale card was scheduled"))
        monkeypatch.setattr(
            review_module,
            "_build_fsrs_manager_from_db",
            forbidden_manager,
        )
        action = asyncio.create_task(
            handler(
                card_id=card_id,
                body=ReviewRequest(rating=3, review_duration_ms=100),
                db_pool=_contract_pool,
                user_id=user_id,
            )
        )
        await asyncio.sleep(0.05)
        assert not action.done()
        release_replacement.set()
        await asyncio.wait_for(replacement, timeout=2)
        with pytest.raises(HTTPException) as exc_info:
            await asyncio.wait_for(action, timeout=2)
        assert exc_info.value.status_code == 409
        forbidden_manager.assert_not_awaited()
        async with _contract_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM review_logs WHERE card_id = $1",
                    card_id,
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM daily_log WHERE user_id = $1",
                    user_id,
                )
                == 0
            )
    finally:
        await _delete_generation_race_fixture(
            _contract_pool,
            user_id=user_id,
            paper_id=paper_id,
            deck_id=deck_id,
        )


async def test_offline_review_and_source_replacement_serialize_in_both_orders(
    _contract_pool,
    monkeypatch,
):
    """Offline replay either commits before replacement or terminally skips after it."""
    import learning_engine.routers.review as review_module
    from learning_engine.models import ReviewSyncRequest

    user_id, paper_id, deck_id, card_id = await _seed_generation_race_card(
        _contract_pool,
        "offline-review-race",
    )
    handler = review_module._sync_reviews
    original_check = review_module._card_source_is_current
    try:
        source_locked = asyncio.Event()
        release_action = asyncio.Event()

        async def gated_check(conn, card):
            current = await original_check(conn, card)
            source_locked.set()
            await release_action.wait()
            return current

        monkeypatch.setattr(review_module, "_card_source_is_current", gated_check)
        first_key = f"offline-before-{uuid.uuid4()}"
        first_body = ReviewSyncRequest(
            reviews=[
                {
                    "idempotency_key": first_key,
                    "card_id": card_id,
                    "rating": 3,
                    "reviewed_at": datetime.now(UTC).isoformat(),
                    "review_duration_ms": 100,
                }
            ]
        )
        action = asyncio.create_task(
            handler(
                body=first_body,
                db_pool=_contract_pool,
                user_id=user_id,
            )
        )
        await asyncio.wait_for(source_locked.wait(), timeout=2)
        replacement_started = asyncio.Event()
        replacement_acquired = asyncio.Event()
        replacement = asyncio.create_task(
            _replace_source(
                _contract_pool,
                paper_id,
                started=replacement_started,
                acquired=replacement_acquired,
            )
        )
        await asyncio.wait_for(replacement_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert not replacement_acquired.is_set()
        release_action.set()
        first = await asyncio.wait_for(action, timeout=2)
        await asyncio.wait_for(replacement, timeout=2)
        assert (first.synced, first.skipped) == (1, 0)

        await _reset_generation_race_card(
            _contract_pool,
            user_id=user_id,
            paper_id=paper_id,
            card_id=card_id,
        )
        replacement_locked = asyncio.Event()
        release_replacement = asyncio.Event()
        replacement = asyncio.create_task(
            _replace_source(
                _contract_pool,
                paper_id,
                acquired=replacement_locked,
                release=release_replacement,
            )
        )
        await asyncio.wait_for(replacement_locked.wait(), timeout=2)
        source_checked = asyncio.Event()

        async def observed_check(conn, card):
            current = await original_check(conn, card)
            source_checked.set()
            return current

        monkeypatch.setattr(review_module, "_card_source_is_current", observed_check)
        second_key = f"offline-after-{uuid.uuid4()}"
        second_body = ReviewSyncRequest(
            reviews=[
                {
                    "idempotency_key": second_key,
                    "card_id": card_id,
                    "rating": 3,
                    "reviewed_at": datetime.now(UTC).isoformat(),
                    "review_duration_ms": 100,
                }
            ]
        )
        action = asyncio.create_task(
            handler(
                body=second_body,
                db_pool=_contract_pool,
                user_id=user_id,
            )
        )
        await asyncio.sleep(0.05)
        assert not action.done()
        assert not source_checked.is_set()
        release_replacement.set()
        await asyncio.wait_for(replacement, timeout=2)
        second = await asyncio.wait_for(action, timeout=2)
        assert (second.synced, second.skipped) == (0, 1)
        async with _contract_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM review_logs WHERE card_id = $1",
                    card_id,
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM daily_log WHERE user_id = $1",
                    user_id,
                )
                == 0
            )
    finally:
        await _delete_generation_race_fixture(
            _contract_pool,
            user_id=user_id,
            paper_id=paper_id,
            deck_id=deck_id,
        )
