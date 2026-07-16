"""sync_reviews concurrent ON CONFLICT race (contract test).

Race surface analysis
---------------------
``sync_reviews`` (review.py:184) processes each review event inside a
per-event transaction with the following sequence:

  1. Pre-flight SELECT of already-applied idempotency_keys (own connection).
  2. Per-event BEGIN SAVEPOINT:
       a. SELECT cards WHERE id=$1 AND user_id=$2 FOR UPDATE   ← row-level lock
       b. INSERT review_logs ON CONFLICT (user_id, idempotency_key)
              WHERE idempotency_key IS NOT NULL DO NOTHING
       c. If inserted_log_id IS NULL → skip (concurrent winner already wrote the row)
       d. UPDATE cards SET fsrs_state=…, due_at=…              ← protected by a-lock

Why the race cannot produce corruption
---------------------------------------
Two concurrent sync requests for the same (user_id, card_id, idempotency_key):

* Step 2a serializes via Postgres row-lock (FOR UPDATE).  Only ONE request can
  hold the lock at a time.  The other blocks until the lock holder commits.
* When the second request finally acquires the lock the idempotency_key is
  already present in review_logs (winner inserted it).  The ON CONFLICT DO
  NOTHING makes inserted_log_id = NULL, so the FSRS state is NOT advanced a
  second time and the event is counted as already_synced.

Without the ON CONFLICT clause (or without the FOR UPDATE lock) the second
request would raise a unique-constraint violation mid-transaction OR silently
double-advance the FSRS state — neither is acceptable.

This test fires N=6 concurrent POST /api/review/sync requests, all for the
SAME (user_id, card_id, idempotency_key).  Expected outcome:
  - All N calls complete without raising.
  - Exactly one review_log row is created (no duplicates, no missing row).
  - The counts across all responses satisfy: sum(synced) == 1,
    sum(already_synced) + sum(synced) == N.
  - The card's fsrs_state is consistent (not None/corrupted).

Note on concurrency mechanism
------------------------------
The ``_le_app`` fixture wraps the app in a ``SharedConnPool`` that serializes
asyncpg access through a reentrant per-task lock.  In practice the N
``asyncio.gather`` tasks interleave at Python-coroutine granularity but
serialize at DB-connection granularity — exactly the same observable behaviour
as N separate OS threads competing for the FOR UPDATE lock, just mediated by
the Python event loop rather than the Postgres lock manager directly.

The ON CONFLICT clause handles both cases: the SharedConnPool path (where the
second caller's pre-flight SELECT already sees the committed row and fast-paths
to already_synced) and the raw-pool path (where two concurrent transactions
both pass the pre-flight check and only the DB constraint decides the winner).
"""

from __future__ import annotations

# Verified: services/learning_engine/learning_engine/routers/review.py:184 — sync_reviews UPSERT + FOR UPDATE card lock
# Verified: services/learning_engine/learning_engine/routers/review.py:231 — INSERT review_logs ON CONFLICT DO NOTHING
# Verified: services/learning_engine/learning_engine/routers/review.py:256 — UPDATE cards WHERE user_id guard

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from jarvis_common.testing_contract_apps import make_contract_client as _client

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_N_CONCURRENT = 6  # number of simultaneous sync requests racing on the same event


# ---------------------------------------------------------------------------
# concurrent upsert does not corrupt
# ---------------------------------------------------------------------------


async def test_sync_reviews_concurrent_upsert_does_not_corrupt(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
) -> None:
    """N concurrent POST /api/review/sync calls for the same event produce exactly one row.

    Verifies the ON CONFLICT (user_id, idempotency_key) DO NOTHING clause plus the
    FOR UPDATE card-lock work together to ensure:
      - No unique-constraint exception escapes the endpoint.
      - Exactly one review_log row is created.
      - The card's fsrs_state is not None / not corrupted.
      - sum(synced) == 1  (only one winner advances FSRS state).
      - sum(already_synced) + sum(synced) == _N_CONCURRENT.
    """
    card_id = contract_two_users.card_id_a
    idem_key = f"concurrent-race-{uuid.uuid4()}"
    reviewed_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    event_payload = {
        "idempotency_key": idem_key,
        "card_id": card_id,
        "rating": 3,
        "reviewed_at": reviewed_at,
        "review_duration_ms": 800,
    }

    async def _one_sync() -> dict:
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/review/sync", json={"reviews": [event_payload]})
        assert resp.status_code == 200, (
            f"sync_reviews returned {resp.status_code}: {resp.text[:300]}"
        )
        return resp.json()

    # Fire N concurrent requests; all must complete without raising.
    results: list[dict] = await asyncio.gather(*[_one_sync() for _ in range(_N_CONCURRENT)])

    # Exactly one winner should have synced == 1; the rest already_synced == 1.
    total_synced = sum(r["synced"] for r in results)
    total_already_synced = sum(r["already_synced"] for r in results)
    total_skipped = sum(r["skipped"] for r in results)

    assert total_skipped == 0, (
        f"No events should be skipped (card_id={card_id} is owned by user_a); "
        f"got skipped counts {[r['skipped'] for r in results]}"
    )
    assert total_synced == 1, (
        f"Exactly one concurrent winner should advance FSRS (synced==1); "
        f"got sum(synced)={total_synced} across {_N_CONCURRENT} requests. "
        f"Individual results: {results}"
    )
    assert total_already_synced == _N_CONCURRENT - 1, (
        f"The remaining {_N_CONCURRENT - 1} requests should report already_synced==1; "
        f"got sum(already_synced)={total_already_synced}. "
        f"Individual results: {results}"
    )

    # DB invariant: exactly ONE review_log row for this idempotency_key.
    row_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM review_logs WHERE idempotency_key = $1 AND user_id = $2",
        idem_key,
        contract_two_users.user_a_id,
    )
    assert row_count == 1, (
        f"Expected exactly 1 review_log row for idempotency_key={idem_key!r}; "
        f"found {row_count} — ON CONFLICT DO NOTHING failed to prevent duplicates"
    )

    # Card state coherence: fsrs_state must not be None (UPDATE ran successfully).
    fsrs_state = await contract_conn.fetchval(
        "SELECT fsrs_state FROM cards WHERE id = $1 AND user_id = $2",
        card_id,
        contract_two_users.user_a_id,
    )
    assert fsrs_state is not None, (
        f"card id={card_id} fsrs_state is None after concurrent sync — "
        "UPDATE did not execute correctly"
    )


async def test_sync_reviews_concurrent_different_keys_all_synced(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
) -> None:
    """N concurrent syncs each with a DISTINCT idempotency_key all succeed independently.

    This is the complementary positive-path test: when each concurrent request carries
    a unique key there is no idempotency collision.  All N should return synced==1.

    Verifies that the serialization introduced by the FOR UPDATE card lock does NOT
    cause any of the concurrent requests to fail — they simply queue and each commits
    its own review_log row.
    """
    card_id = contract_two_users.card_id_a
    reviewed_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

    async def _one_sync_unique() -> dict:
        event_payload = {
            "idempotency_key": f"concurrent-unique-{uuid.uuid4()}",
            "card_id": card_id,
            "rating": 3,
            "reviewed_at": reviewed_at,
            "review_duration_ms": 900,
        }
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/review/sync", json={"reviews": [event_payload]})
        assert resp.status_code == 200, (
            f"sync_reviews returned {resp.status_code}: {resp.text[:300]}"
        )
        return resp.json()

    results: list[dict] = await asyncio.gather(*[_one_sync_unique() for _ in range(_N_CONCURRENT)])

    total_synced = sum(r["synced"] for r in results)
    total_already_synced = sum(r["already_synced"] for r in results)

    assert total_synced == _N_CONCURRENT, (
        f"Each of the {_N_CONCURRENT} unique-key requests should sync independently; "
        f"got sum(synced)={total_synced}, sum(already_synced)={total_already_synced}. "
        f"Individual results: {results}"
    )
    assert total_already_synced == 0, (
        f"No request should be deduplicated when all keys are distinct; "
        f"got sum(already_synced)={total_already_synced}"
    )

    # DB invariant: N distinct review_log rows created.
    log_rows = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM review_logs "
        "WHERE idempotency_key LIKE 'concurrent-unique-%' AND user_id = $1",
        contract_two_users.user_a_id,
    )
    assert log_rows == _N_CONCURRENT, (
        f"Expected {_N_CONCURRENT} review_log rows for distinct concurrent syncs; found {log_rows}"
    )


# ---------------------------------------------------------------------------
# naive reviewed_at counts on the correct UTC day (end-to-end _to_utc proof)
# ---------------------------------------------------------------------------
#
# Verified: services/learning_engine/learning_engine/routers/review.py (sync_reviews) —
#   reviewed_at_utc = _to_utc(event.reviewed_at).astimezone(UTC); the daily_log
#   upsert runs inside the per-event transaction, keyed by reviewed_at_utc.date(),
#   so each event increments daily_log.cards_reviewed for the UTC day it occurred on.


async def test_sync_reviews_naive_reviewed_at_counts_on_utc_day(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
) -> None:
    """A sync event whose ``reviewed_at`` is a NAIVE datetime (no tzinfo/offset)
    representing "now" in UTC must be counted on the correct UTC day.

    This is the end-to-end proof that the ``_to_utc`` normalisation actually
    fixes the streak/day-counting bug: the route reads ``reviewed_at`` (parsed
    by pydantic into a naive datetime, exactly as a JS client sending an
    offset-less ISO string would produce), normalises it to UTC, and increments
    today's ``daily_log.cards_reviewed`` only when it lands on the current UTC
    day.  If the naive value were mishandled (treated as some other zone, or the
    comparison crashed) the day's count would be wrong / the row absent.
    """
    card_id = contract_two_users.card_id_a
    user_a_id = contract_two_users.user_a_id

    # Naive "now" in UTC — no tzinfo, no offset suffix. e.g. "2026-06-02T12:34:56".
    naive_now_utc = datetime.now(UTC).replace(tzinfo=None).isoformat()
    assert "+" not in naive_now_utc and "Z" not in naive_now_utc, (
        "reviewed_at under test must be naive (no tz offset) to prove the fix"
    )

    event_payload = {
        "idempotency_key": f"naive-utc-{uuid.uuid4()}",
        "card_id": card_id,
        "rating": 3,
        "reviewed_at": naive_now_utc,
        "review_duration_ms": 700,
    }

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/review/sync", json={"reviews": [event_payload]})
    assert resp.status_code == 200, f"sync_reviews returned {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["synced"] == 1, f"naive-UTC event should sync exactly once; got {body}"

    # Observable: the event is counted on the current UTC day in daily_log.
    # The seed creates no daily_log rows, so a row here proves the increment fired.
    cards_reviewed = await contract_conn.fetchval(
        "SELECT cards_reviewed FROM daily_log "
        "WHERE user_id = $1 AND log_date = (NOW() AT TIME ZONE 'UTC')::date",
        user_a_id,
    )
    assert cards_reviewed == 1, (
        "a naive reviewed_at == now(UTC) must be counted on the current UTC day "
        f"(daily_log.cards_reviewed for CURRENT_DATE); got {cards_reviewed!r}"
    )


# ---------------------------------------------------------------------------
# out-of-order guard: a stale (older) review must never rewind card scheduling
# ---------------------------------------------------------------------------
#
# Verified: services/learning_engine/learning_engine/routers/review.py (sync_reviews) —
#   prior_last = MAX(review_logs.reviewed_at) is read under the card's FOR UPDATE lock
#   BEFORE the current event's log is inserted; the `UPDATE cards SET fsrs_state, due_at`
#   only runs when NOT is_stale, so an older review records history without moving
#   `due_at` / `fsrs_state->>'last_review'` backwards.


async def _post_sync(_le_app, cookie, event_payload) -> dict:
    async with _client(_le_app, cookie) as c:
        resp = await c.post("/api/review/sync", json={"reviews": [event_payload]})
    assert resp.status_code == 200, f"sync_reviews returned {resp.status_code}: {resp.text[:300]}"
    return resp.json()


def _sync_event(card_id, reviewed_at, key_prefix) -> dict:
    return {
        "idempotency_key": f"{key_prefix}-{uuid.uuid4()}",
        "card_id": card_id,
        "rating": 3,
        "reviewed_at": reviewed_at.isoformat(),
        "review_duration_ms": 800,
    }


async def _read_card_schedule(contract_conn, card_id, user_id):
    row = await contract_conn.fetchrow(
        "SELECT due_at, fsrs_state->>'last_review' AS last_review "
        "FROM cards WHERE id = $1 AND user_id = $2",
        card_id,
        user_id,
    )
    return row["due_at"], row["last_review"]


async def test_sync_reviews_stale_replay_does_not_rewind_schedule(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
) -> None:
    """A DISTINCT older review posted in a LATER request must not rewind the card.

    Regression for the chronology defect: ``sync_reviews`` used to overwrite
    ``cards.fsrs_state``/``due_at`` unconditionally, so a distinct older event
    arriving after a newer one re-anchored FSRS at the older time and moved
    scheduling backwards.  With the guard the older event is still recorded in
    ``review_logs`` (synced == 1) but the card's persisted schedule is unchanged.

    FAILS ON BASE: the older event's ``schedule_review`` anchored at T1 overwrites
    the card, so ``due_at`` drops below ``due2`` and ``last_review`` regresses to T1.
    """
    card_id = contract_two_users.card_id_a
    user_a_id = contract_two_users.user_a_id
    now = datetime.now(UTC)

    # Newer review first — establishes the card's authoritative schedule.
    newer = await _post_sync(
        _le_app,
        contract_two_users.cookie_a,
        _sync_event(card_id, now - timedelta(hours=1), "stale-newer"),
    )
    assert newer["synced"] == 1, f"newer event should sync once; got {newer}"
    due2, last_review2 = await _read_card_schedule(contract_conn, card_id, user_a_id)
    assert due2 is not None and last_review2 is not None

    # Distinct OLDER review, same card, unique key, in a separate request.
    older = await _post_sync(
        _le_app,
        contract_two_users.cookie_a,
        _sync_event(card_id, now - timedelta(hours=25), "stale-older"),
    )
    assert older["synced"] == 1, (
        f"stale older event is still newly written to review_logs (synced==1); got {older}"
    )

    due_after, last_review_after = await _read_card_schedule(contract_conn, card_id, user_a_id)
    assert due_after == due2, (
        f"stale older review rewound due_at: {due_after} != {due2} (must not regress)"
    )
    assert datetime.fromisoformat(last_review_after) == datetime.fromisoformat(last_review2), (
        f"stale older review rewound last_review to the older time: "
        f"{last_review_after!r} != {last_review2!r}"
    )


async def test_sync_reviews_concurrent_out_of_order_reflects_newer(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
) -> None:
    """Newer and older reviews racing on the SAME card converge on the newer schedule.

    Fired concurrently via ``asyncio.gather`` with distinct keys; the FOR UPDATE lock
    serializes them and the staleness guard makes the outcome order-independent: the
    persisted ``last_review`` is always the newer event T2, never the older T1.

    ON BASE this fails whenever the older transaction commits last (it overwrites the
    card with scheduling anchored at T1).
    """
    card_id = contract_two_users.card_id_a
    user_a_id = contract_two_users.user_a_id
    now = datetime.now(UTC)
    t2_newer = now - timedelta(hours=1)
    t1_older = now - timedelta(hours=25)

    newer_event = _sync_event(card_id, t2_newer, "race-newer")
    older_event = _sync_event(card_id, t1_older, "race-older")

    results = await asyncio.gather(
        _post_sync(_le_app, contract_two_users.cookie_a, newer_event),
        _post_sync(_le_app, contract_two_users.cookie_a, older_event),
    )
    assert sum(r["synced"] for r in results) == 2, (
        f"both distinct-key events are newly recorded (synced total == 2); got {results}"
    )

    _, last_review = await _read_card_schedule(contract_conn, card_id, user_a_id)
    persisted = datetime.fromisoformat(last_review)
    assert persisted == t2_newer, (
        f"persisted last_review must reflect the newer review {t2_newer.isoformat()}, "
        f"got {last_review!r}"
    )
    assert persisted != t1_older, (
        f"persisted last_review must never reflect the older review {t1_older.isoformat()}"
    )
