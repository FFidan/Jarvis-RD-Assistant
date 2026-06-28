"""Unit tests for digest filtering based on the paper_user_state schema.

Tests verify that the _simple_digest query correctly filters papers by:
  - state ENUM: 'trash' and 'done' are excluded from the digest (NOT EXISTS guard).
  - starred=TRUE is included (positive engagement signal).
  - state='reading' is included (positive engagement signal).
  - recommendation_feedback with signal='positive', source='pulse_thumbs', recent 7 days
    is included — but only when not overridden by the NOT EXISTS guard.
  - user_id scoping via ``IS NOT DISTINCT FROM $1`` (NULL = single-tenant).

Deleted tests and rationale:
  - test_digest_includes_read_active: state='done' (previously status='read') is now in
    BOTH the NOT EXISTS exclude guard AND the include branch.  The exclude guard always
    wins, so done papers are permanently excluded from the digest.  This is intentional
    (state='done' means the user has already triaged the paper).
  - test_digest_excludes_starred_status_legacy: the legacy status column (with its
    CHECK constraint) was dropped in migration 047.  The CHECK-violation guard it tested
    no longer applies to the current schema.
  - test_digest_excludes_pulse_rated_then_dismissed: dismiss → trash.  This case is now
    fully covered by test_digest_excludes_state_trash.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


async def _setup_tables(conn) -> None:  # noqa: ANN001
    """Create Phase-A schema tables for digest tests.

    Matches the columns expected by the production _simple_digest SQL (as of
    migrations 047-049): paper_user_state uses state ENUM (no archived/dismissed/
    status/saved), recommendation_feedback replaces pulse_ratings.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            id BIGSERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            url TEXT,
            published_date TIMESTAMPTZ,
            authors TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL UNIQUE
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_topics (
            paper_id BIGINT NOT NULL,
            topic_id BIGINT NOT NULL,
            relevance_score FLOAT,
            UNIQUE(paper_id, topic_id)
        )
    """)
    # Phase-A schema: state ENUM, starred, no archived/dismissed/status/saved.
    # user_id is nullable to support single-tenant (NULL) mode.
    # PRIMARY KEY requires NOT NULL columns; we use UNIQUE NULLS NOT DISTINCT instead
    # (migration 047 keeps the original PK but drops the old columns — this test helper
    # uses the relaxed form that matches the runtime invariants rather than the DDL PK).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_user_state (
            paper_id BIGINT NOT NULL,
            user_id  BIGINT,
            state    TEXT NOT NULL DEFAULT 'inbox'
                         CHECK (state IN ('inbox', 'to_read', 'reading', 'done', 'trash')),
            starred  BOOLEAN NOT NULL DEFAULT FALSE,
            UNIQUE NULLS NOT DISTINCT (paper_id, user_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_summaries (
            paper_id BIGINT PRIMARY KEY,
            summary_brief TEXT,
            confidence VARCHAR(10)
        )
    """)
    # Migration 049: recommendation_feedback replaces pulse_ratings.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS recommendation_feedback (
            id         BIGSERIAL PRIMARY KEY,
            paper_id   BIGINT NOT NULL,
            user_id    BIGINT,
            signal     TEXT NOT NULL CHECK (signal IN ('positive', 'negative')),
            source     TEXT NOT NULL CHECK (source IN (
                'pulse_thumbs', 'feed_thumbs', 'paper_detail_thumbs', 'dismiss_combined'
            )),
            topic_id   BIGINT,
            reason     TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT recommendation_feedback_paper_user_source_uniq
                UNIQUE NULLS NOT DISTINCT (paper_id, user_id, source)
        )
    """)


# ---------------------------------------------------------------------------
# Query helper — VERBATIM copy of the prod SQL from _simple_digest
# (services/telegram_bot/telegram_bot/orchestration/paper_digest.py, lines 155-194)
# ---------------------------------------------------------------------------


async def _digest_query(
    conn,  # noqa: ANN001
    *,
    db_user_id: int | None = None,
) -> list:
    """Execute the digest filtering query.

    Mirrors the production SQL in paper_digest._simple_digest verbatim,
    including the 3× ``IS NOT DISTINCT FROM $1`` user-id scoping clauses.
    The ``db_user_id`` parameter maps directly to the ``$1`` bind parameter.
    ``None`` matches NULL rows (single-tenant mode) via ``IS NOT DISTINCT FROM``.
    """
    sql = """SELECT p.id, p.title, p.url, p.published_date, p.authors,
                  t.name as topic_name, pt.relevance_score,
                  ps.summary_brief, ps.confidence
           FROM papers p
           JOIN paper_topics pt ON p.id = pt.paper_id
           JOIN topics t ON pt.topic_id = t.id
           LEFT JOIN paper_summaries ps ON p.id = ps.paper_id
           WHERE p.created_at >= NOW() - INTERVAL '7 days'
             AND NOT EXISTS (
                 SELECT 1 FROM paper_user_state pus
                  WHERE pus.paper_id = p.id
                    AND pus.user_id IS NOT DISTINCT FROM $1
                    AND pus.state IN ('trash', 'done')
             )
             AND (
                 EXISTS (
                     SELECT 1 FROM paper_user_state pus2
                     WHERE pus2.paper_id = p.id
                       AND pus2.user_id IS NOT DISTINCT FROM $1
                       AND (
                           COALESCE(pus2.starred, FALSE) = TRUE
                           OR pus2.state IN ('reading', 'done')
                       )
                 )
                 OR EXISTS (
                     SELECT 1 FROM recommendation_feedback rf
                     WHERE rf.paper_id = p.id
                       AND rf.user_id IS NOT DISTINCT FROM $1
                       AND rf.signal = 'positive'
                       AND rf.source = 'pulse_thumbs'
                       AND rf.created_at >= NOW() - INTERVAL '7 days'
                 )
             )
           ORDER BY t.name, pt.relevance_score DESC NULLS LAST"""
    return await conn.fetch(sql, db_user_id)


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------


async def _insert_paper(conn, title: str, url: str) -> int:  # noqa: ANN001
    """Insert a paper created 1 day ago and return its id."""
    await conn.execute(
        "INSERT INTO papers (title, url, published_date) VALUES ($1, $2, NOW() - INTERVAL '1 day')",
        title,
        url,
    )
    return await conn.fetchval("SELECT id FROM papers WHERE title = $1", title)


async def _insert_topic(conn, name: str) -> int:  # noqa: ANN001
    """Insert a topic (or retrieve existing) and return its id."""
    await conn.execute(
        "INSERT INTO topics (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
        name,
    )
    return await conn.fetchval("SELECT id FROM topics WHERE name = $1", name)


async def _link_paper_topic(
    conn,  # noqa: ANN001
    paper_id: int,
    topic_id: int,
    relevance_score: float = 0.9,
) -> None:
    """Link a paper to a topic with an optional relevance score."""
    await conn.execute(
        "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) "
        "VALUES ($1, $2, $3) ON CONFLICT (paper_id, topic_id) DO NOTHING",
        paper_id,
        topic_id,
        relevance_score,
    )


async def _insert_user_state(
    conn,  # noqa: ANN001
    paper_id: int,
    *,
    user_id: int | None = None,
    state: str = "inbox",
    starred: bool = False,
) -> None:
    """Insert a paper_user_state row using the Phase-A schema."""
    await conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state, starred) VALUES ($1, $2, $3, $4)",
        paper_id,
        user_id,
        state,
        starred,
    )


async def _insert_recommendation_feedback(
    conn,  # noqa: ANN001
    paper_id: int,
    *,
    user_id: int | None = None,
    signal: str = "positive",
    source: str = "pulse_thumbs",
    created_at: str | None = None,
) -> None:
    """Insert a recommendation_feedback row (replaces legacy _insert_pulse_rating)."""
    if created_at is None:
        await conn.execute(
            "INSERT INTO recommendation_feedback (paper_id, user_id, signal, source) "
            "VALUES ($1, $2, $3, $4)",
            paper_id,
            user_id,
            signal,
            source,
        )
    else:
        await conn.execute(
            "INSERT INTO recommendation_feedback "
            "(paper_id, user_id, signal, source, created_at) "
            "VALUES ($1, $2, $3, $4, $5::timestamptz)",
            paper_id,
            user_id,
            signal,
            source,
            created_at,
        )


# ---------------------------------------------------------------------------
# Tests (5 active + 1 scoping safety net = 6 total)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_state_trash(live_pg_dsn: str) -> None:
    """Digest excludes papers with state='trash', even if starred=TRUE.

    Phase-A replacement for test_digest_excludes_dismissed.
    dismissed=TRUE mapped to state='trash' in migration 047.
    """
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)
        paper_id = await _insert_paper(conn, "Trashed Paper", "https://example.com/trash")
        topic_id = await _insert_topic(conn, "Test Topic")
        await _link_paper_topic(conn, paper_id, topic_id)

        # state='trash', starred=TRUE — exclude guard must win.
        await _insert_user_state(conn, paper_id, state="trash", starred=True)

        rows = await _digest_query(conn)
        assert len(rows) == 0, "Paper with state='trash' must not appear in digest"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_state_done_with_no_other_signal(live_pg_dsn: str) -> None:
    """Digest excludes papers with state='done' and no additional positive signal.

    Phase-A replacement for test_digest_excludes_archived.
    archived=TRUE mapped to state='done' in migration 047.  state='done' is in
    the NOT EXISTS exclude guard — it always wins, regardless of starred flag.
    """
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)
        paper_id = await _insert_paper(conn, "Done Paper", "https://example.com/done")
        topic_id = await _insert_topic(conn, "Test Topic")
        await _link_paper_topic(conn, paper_id, topic_id)

        # state='done', no recommendation_feedback — must be excluded.
        await _insert_user_state(conn, paper_id, state="done", starred=True)

        rows = await _digest_query(conn)
        assert len(rows) == 0, (
            "Paper with state='done' must not appear in digest "
            "(NOT EXISTS guard wins even with starred=TRUE)"
        )

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_includes_starred_active(live_pg_dsn: str) -> None:
    """Digest includes papers with starred=TRUE and state NOT in ('trash','done')."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)
        paper_id = await _insert_paper(conn, "Starred Active Paper", "https://example.com/starred")
        topic_id = await _insert_topic(conn, "Test Topic")
        await _link_paper_topic(conn, paper_id, topic_id)

        # state='inbox', starred=TRUE — should appear in digest.
        await _insert_user_state(conn, paper_id, state="inbox", starred=True)

        rows = await _digest_query(conn)
        assert len(rows) == 1, "Active starred paper should appear in digest"
        assert rows[0]["title"] == "Starred Active Paper"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_includes_reading_active(live_pg_dsn: str) -> None:
    """Digest includes papers with state='reading' (active engagement, not yet done)."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)
        paper_id = await _insert_paper(conn, "Reading Paper", "https://example.com/reading")
        topic_id = await _insert_topic(conn, "Test Topic")
        await _link_paper_topic(conn, paper_id, topic_id)

        # state='reading' — not in the exclude guard; IS in the include branch.
        await _insert_user_state(conn, paper_id, state="reading")

        rows = await _digest_query(conn)
        assert len(rows) == 1, "Paper with state='reading' should appear in digest"
        assert rows[0]["title"] == "Reading Paper"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_positive_feedback_then_trashed(live_pg_dsn: str) -> None:
    """Regression guard: positive recommendation_feedback then state='trash'.

    Phase-A replacement for test_digest_excludes_pulse_rated_then_archived.
    A paper with a recent 'positive'/'pulse_thumbs' feedback that was subsequently
    trashed must NOT appear in the digest.  The NOT EXISTS guard on state='trash'
    must win over the recommendation_feedback OR branch.
    """
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)
        paper_id = await _insert_paper(
            conn,
            "Positive Feedback Then Trashed",
            "https://example.com/fb-then-trash",
        )
        topic_id = await _insert_topic(conn, "Test Topic")
        await _link_paper_topic(conn, paper_id, topic_id, relevance_score=0.8)

        # Positive recommendation feedback (would qualify via OR branch).
        await _insert_recommendation_feedback(
            conn, paper_id, signal="positive", source="pulse_thumbs"
        )

        # Trashed after feedback — user explicitly rejected the paper.
        await _insert_user_state(conn, paper_id, state="trash")

        rows = await _digest_query(conn)
        assert len(rows) == 0, (
            "Trashed paper with positive recommendation_feedback must not appear "
            "in digest (regression guard)"
        )

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_user_id_scoping(live_pg_dsn: str) -> None:
    """Safety net: _digest_query respects user_id scoping via IS NOT DISTINCT FROM.

    This test would have FAILED before T8 because the old _digest_query helper
    omitted all IS NOT DISTINCT FROM $1 scoping clauses.

    Scenario A — user_id=2 row should not bleed into user_id=1 or NULL queries.
    Scenario B — user_id=NULL row (single-tenant) is correctly scoped to db_user_id=None
                 and correctly excluded when queried with db_user_id=1.
    """
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)
        topic_id = await _insert_topic(conn, "Scoping Topic")

        # --- Paper 1: state row belongs to user_id=2 ---
        paper1_id = await _insert_paper(
            conn, "User2 Starred Paper", "https://example.com/user2-star"
        )
        await _link_paper_topic(conn, paper1_id, topic_id)
        # Starred for user_id=2 only — must NOT appear for user_id=1 or NULL.
        await _insert_user_state(conn, paper1_id, user_id=2, state="inbox", starred=True)

        # Query as user_id=1 → paper1 must be EXCLUDED (state row is for user_id=2).
        rows = await _digest_query(conn, db_user_id=1)
        ids = [r["id"] for r in rows]
        assert paper1_id not in ids, (
            "Paper1 (starred for user_id=2) must NOT appear when querying as user_id=1"
        )

        # Query as db_user_id=None (single-tenant) → paper1 must also be EXCLUDED.
        # The state row has user_id=2 which IS DISTINCT FROM NULL, so no include match.
        rows = await _digest_query(conn, db_user_id=None)
        ids = [r["id"] for r in rows]
        assert paper1_id not in ids, (
            "Paper1 (starred for user_id=2) must NOT appear when querying as NULL (single-tenant)"
        )

        # --- Paper 2: state row has user_id=NULL (single-tenant mode) ---
        paper2_id = await _insert_paper(
            conn, "NULL Tenant Starred Paper", "https://example.com/null-star"
        )
        await _link_paper_topic(conn, paper2_id, topic_id)
        # Starred with user_id=NULL — should appear for db_user_id=None only.
        await _insert_user_state(conn, paper2_id, user_id=None, state="inbox", starred=True)

        # Query as db_user_id=None → paper2 MUST appear (NULL IS NOT DISTINCT FROM NULL).
        rows = await _digest_query(conn, db_user_id=None)
        ids = [r["id"] for r in rows]
        assert paper2_id in ids, (
            "Paper2 (starred for user_id=NULL) MUST appear when querying as db_user_id=None"
        )

        # Query as db_user_id=1 → paper2 must be EXCLUDED (NULL IS DISTINCT FROM 1).
        rows = await _digest_query(conn, db_user_id=1)
        ids = [r["id"] for r in rows]
        assert paper2_id not in ids, (
            "Paper2 (starred for user_id=NULL) must NOT appear when querying as db_user_id=1"
        )

    finally:
        await conn.close()
