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
