"""Live-PG contract test for db_helpers.record_author_alert.

Proves the ON CONFLICT DO NOTHING deduplication semantics that the AsyncMock-
based unit tests in test_record_author_alert.py cannot exercise:

  1. First call with a new (tracked_author_id, paper_id, user_id) triple → True.
  2. Second call with the same triple → False (conflict skipped the row).
  3. author_alert_log contains exactly 1 row for that triple.
  4. Two distinct triples both succeed independently (positive sanity).

Grounding:
  db_helpers.py:367-387  — record_author_alert uses RETURNING to detect insert vs skip.
  db/init.sql:492-498    — author_alert_log columns (id, tracked_author_id, paper_id,
                            notified_at, user_id); user_id INTEGER.
  db/init.sql:1636       — UNIQUE INDEX author_alert_log_dedupe (tracked_author_id,
                            paper_id, user_id).
  db/init.sql:1754-1759  — FK: paper_id → papers, tracked_author_id → tracked_authors,
                            user_id → users ON DELETE SET NULL.
  db/init.sql:1365-1373  — users.id BIGINT; cast to int for author_alert_log.user_id.
  db/init.sql:1316-1326  — tracked_authors: id SERIAL, user_id INTEGER, author_name TEXT,
                            source VARCHAR(20) default 'manual'.
  db/init.sql:927-949    — papers: external_id, source_type, title, authors[], url required.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_triple(conn) -> tuple[int, int, int]:
    """Insert a user, paper, and tracked_author row; return (tracked_author_id, paper_id, user_id).

    Generates unique identifiers via uuid to avoid cross-test collisions within
    the rolled-back transaction.
    """
    tag = uuid.uuid4().hex[:8]

    user_id: int = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        f"alert-{tag}@contract.example.com",
    )

    paper_id: int = await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'Alert Contract Paper', ARRAY['A. Author'],
                   $2, $3)
           RETURNING id""",
        f"alert-ext-{tag}",
        f"https://example.test/alert-{tag}",
        user_id,
    )

    tracked_author_id: int = await conn.fetchval(
        """INSERT INTO tracked_authors (author_name, user_id)
           VALUES ($1, $2)
           RETURNING id""",
        f"Test Author {tag}",
        int(user_id),  # tracked_authors.user_id is INTEGER
    )

    return int(tracked_author_id), int(paper_id), int(user_id)


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


async def test_record_author_alert_first_call_returns_true(contract_conn) -> None:
    """First insert with a new triple returns True.

    Verified: db_helpers.py:378-387 — RETURNING tracked_author_id; row is not None → True.
    """
    from jarvis_common.db_helpers import record_author_alert

    tracked_author_id, paper_id, user_id = await _seed_triple(contract_conn)
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_research_runtime")

    result = await record_author_alert(
        contract_conn,
        tracked_author_id=tracked_author_id,
        paper_id=paper_id,
        user_id=user_id,
    )

    assert result is True, "First insert should return True"


async def test_record_author_alert_second_call_returns_false(contract_conn) -> None:
    """Second call with the same triple returns False (ON CONFLICT DO NOTHING fired).

    Verified: db_helpers.py:381 — ON CONFLICT (tracked_author_id, paper_id, user_id) DO NOTHING.
    Verified: db/init.sql:1636 — UNIQUE INDEX author_alert_log_dedupe covers this triple.
    """
    from jarvis_common.db_helpers import record_author_alert

    tracked_author_id, paper_id, user_id = await _seed_triple(contract_conn)
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_research_runtime")

    first = await record_author_alert(
        contract_conn,
        tracked_author_id=tracked_author_id,
        paper_id=paper_id,
        user_id=user_id,
    )
    second = await record_author_alert(
        contract_conn,
        tracked_author_id=tracked_author_id,
        paper_id=paper_id,
        user_id=user_id,
    )

    assert first is True, "First call should return True"
    assert second is False, "Second call with same triple should return False"


async def test_record_author_alert_exactly_one_row_after_duplicate(contract_conn) -> None:
    """Exactly one row exists in author_alert_log after two calls with the same triple.

    Proves the DB-level deduplication is structurally enforced, not just
    reflected in the return value.
    """
    from jarvis_common.db_helpers import record_author_alert

    tracked_author_id, paper_id, user_id = await _seed_triple(contract_conn)
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_research_runtime")

    await record_author_alert(
        contract_conn,
        tracked_author_id=tracked_author_id,
        paper_id=paper_id,
        user_id=user_id,
    )
    await record_author_alert(
        contract_conn,
        tracked_author_id=tracked_author_id,
        paper_id=paper_id,
        user_id=user_id,
    )

    count: int = await contract_conn.fetchval(
        """SELECT count(*)
           FROM author_alert_log
           WHERE tracked_author_id = $1
             AND paper_id = $2
             AND user_id = $3""",
        tracked_author_id,
        paper_id,
        user_id,
    )

    assert count == 1, f"Expected exactly 1 row; found {count}"


async def test_record_author_alert_distinct_triples_both_succeed(contract_conn) -> None:
    """Two distinct triples both insert successfully (positive sanity).

    Verifies that deduplication is keyed on the full triple and does not
    accidentally block unrelated rows.
    """
    from jarvis_common.db_helpers import record_author_alert

    triple_a = await _seed_triple(contract_conn)
    triple_b = await _seed_triple(contract_conn)
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_research_runtime")

    result_a = await record_author_alert(
        contract_conn,
        tracked_author_id=triple_a[0],
        paper_id=triple_a[1],
        user_id=triple_a[2],
    )
    result_b = await record_author_alert(
        contract_conn,
        tracked_author_id=triple_b[0],
        paper_id=triple_b[1],
        user_id=triple_b[2],
    )

    assert result_a is True, "First distinct triple should return True"
    assert result_b is True, "Second distinct triple should also return True"
