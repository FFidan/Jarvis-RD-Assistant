"""Unit tests for digest filtering based on paper_user_state flags.

Tests verify that the _simple_digest query correctly filters papers by:
  - archived=FALSE (excludes archived papers)
  - dismissed=FALSE (excludes dismissed papers)
  - status IN ('reading', 'read') OR starred=TRUE (includes active reading or starred)
  - Rejects legacy status='starred' (migration 046 CHECK constraint)
"""

from __future__ import annotations

import pytest


async def _setup_tables(conn) -> None:
    """Create schema tables for digest tests."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            url TEXT,
            published_date TIMESTAMPTZ,
            authors TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL UNIQUE
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_topics (
            paper_id INT NOT NULL,
            topic_id INT NOT NULL,
            relevance_score FLOAT,
            UNIQUE(paper_id, topic_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_user_state (
            paper_id INT NOT NULL,
            user_id INT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'reading', 'read')),
            starred BOOLEAN NOT NULL DEFAULT FALSE,
            archived BOOLEAN NOT NULL DEFAULT FALSE,
            dismissed BOOLEAN NOT NULL DEFAULT FALSE,
            saved BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY(paper_id, user_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_summaries (
            paper_id INT PRIMARY KEY,
            summary_brief TEXT,
            confidence VARCHAR(10)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pulse_ratings (
            paper_id INT NOT NULL,
            user_id INT,
            rating VARCHAR(10),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


async def _digest_query(conn) -> list:
    """Execute the digest filtering query (mirrors production SQL in paper_digest.py).

    Archived/dismissed is a top-level NOT EXISTS guard so it applies to both
    the paper_user_state branch AND the pulse_ratings branch (NEW-M11 fix).
    """
    sql = """
        SELECT p.id, p.title, p.url, p.published_date, p.authors,
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
                 AND (COALESCE(pus.archived, FALSE) OR COALESCE(pus.dismissed, FALSE))
          )
          AND (
              EXISTS (
                  SELECT 1 FROM paper_user_state pus2
                  WHERE pus2.paper_id = p.id
                    AND (COALESCE(pus2.starred, FALSE)
                         OR pus2.status IN ('reading', 'read'))
              )
              OR EXISTS (
                  SELECT 1 FROM pulse_ratings pr
                  WHERE pr.paper_id = p.id
                    AND pr.rating IN ('up', 'save', 'open')
                    AND pr.created_at >= NOW() - INTERVAL '7 days'
              )
          )
        ORDER BY t.name, pt.relevance_score DESC NULLS LAST
    """
    return await conn.fetch(sql)


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_dismissed(live_pg_dsn: str) -> None:
    """Digest excludes papers marked dismissed, even if starred=TRUE."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        # Insert test paper
        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Dismissed Paper",
            "https://example.com/dismissed",
        )
        paper_id = await conn.fetchval("SELECT id FROM papers WHERE title = 'Dismissed Paper'")

        # Create topic
        await conn.execute("INSERT INTO topics (name) VALUES ($1)", "Test Topic")
        topic_id = await conn.fetchval("SELECT id FROM topics WHERE name = 'Test Topic'")

        # Link paper to topic
        await conn.execute(
            "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, $3)",
            paper_id,
            topic_id,
            0.9,
        )

        # Mark paper as starred but dismissed
        await conn.execute(
            "INSERT INTO paper_user_state "
            "(paper_id, user_id, status, starred, dismissed) "
            "VALUES ($1, $2, $3, $4, $5)",
            paper_id,
            999,
            "read",
            True,
            True,
        )

        rows = await _digest_query(conn)
        assert len(rows) == 0, "Dismissed paper should not appear in digest"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_archived(live_pg_dsn: str) -> None:
    """Digest excludes papers marked archived, even if starred=TRUE."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        # Insert test paper
        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Archived Paper",
            "https://example.com/archived",
        )
        paper_id = await conn.fetchval("SELECT id FROM papers WHERE title = 'Archived Paper'")

        # Create topic
        await conn.execute("INSERT INTO topics (name) VALUES ($1)", "Test Topic")
        topic_id = await conn.fetchval("SELECT id FROM topics WHERE name = 'Test Topic'")

        # Link paper to topic
        await conn.execute(
            "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, $3)",
            paper_id,
            topic_id,
            0.9,
        )

        # Mark paper as starred but archived
        await conn.execute(
            "INSERT INTO paper_user_state "
            "(paper_id, user_id, status, starred, archived) "
            "VALUES ($1, $2, $3, $4, $5)",
            paper_id,
            999,
            "read",
            True,
            True,
        )

        rows = await _digest_query(conn)
        assert len(rows) == 0, "Archived paper should not appear in digest"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_includes_starred_active(live_pg_dsn: str) -> None:
    """Digest includes papers with starred=TRUE and no archived/dismissed flags."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        # Insert test paper
        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Starred Active Paper",
            "https://example.com/starred",
        )
        paper_id = await conn.fetchval("SELECT id FROM papers WHERE title = 'Starred Active Paper'")

        # Create topic
        await conn.execute("INSERT INTO topics (name) VALUES ($1)", "Test Topic")
        topic_id = await conn.fetchval("SELECT id FROM topics WHERE name = 'Test Topic'")

        # Link paper to topic
        await conn.execute(
            "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, $3)",
            paper_id,
            topic_id,
            0.9,
        )

        # Mark paper as starred, not archived/dismissed
        await conn.execute(
            "INSERT INTO paper_user_state "
            "(paper_id, user_id, status, starred) "
            "VALUES ($1, $2, $3, $4)",
            paper_id,
            999,
            "new",
            True,
        )

        rows = await _digest_query(conn)
        assert len(rows) == 1, "Active starred paper should appear in digest"
        assert rows[0]["title"] == "Starred Active Paper"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_includes_reading_active(live_pg_dsn: str) -> None:
    """Digest includes papers with status='reading' and no archived/dismissed."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        # Insert test paper
        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Reading Paper",
            "https://example.com/reading",
        )
        paper_id = await conn.fetchval("SELECT id FROM papers WHERE title = 'Reading Paper'")

        # Create topic
        await conn.execute("INSERT INTO topics (name) VALUES ($1)", "Test Topic")
        topic_id = await conn.fetchval("SELECT id FROM topics WHERE name = 'Test Topic'")

        # Link paper to topic
        await conn.execute(
            "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, $3)",
            paper_id,
            topic_id,
            0.9,
        )

        # Mark paper as reading
        await conn.execute(
            "INSERT INTO paper_user_state "
            "(paper_id, user_id, status, saved) "
            "VALUES ($1, $2, $3, $4)",
            paper_id,
            999,
            "reading",
            True,
        )

        rows = await _digest_query(conn)
        assert len(rows) == 1, "Reading status paper should appear in digest"
        assert rows[0]["title"] == "Reading Paper"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_includes_read_active(live_pg_dsn: str) -> None:
    """Digest includes papers with status='read' and no archived/dismissed."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        # Insert test paper
        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Read Paper",
            "https://example.com/read",
        )
        paper_id = await conn.fetchval("SELECT id FROM papers WHERE title = 'Read Paper'")

        # Create topic
        await conn.execute("INSERT INTO topics (name) VALUES ($1)", "Test Topic")
        topic_id = await conn.fetchval("SELECT id FROM topics WHERE name = 'Test Topic'")

        # Link paper to topic
        await conn.execute(
            "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, $3)",
            paper_id,
            topic_id,
            0.9,
        )

        # Mark paper as read
        await conn.execute(
            "INSERT INTO paper_user_state (paper_id, user_id, status) VALUES ($1, $2, $3)",
            paper_id,
            999,
            "read",
        )

        rows = await _digest_query(conn)
        assert len(rows) == 1, "Read status paper should appear in digest"
        assert rows[0]["title"] == "Read Paper"

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_starred_status_legacy(live_pg_dsn: str) -> None:
    """Confirm status='starred' is rejected (migration 046 CHECK constraint)."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        # Insert test paper
        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Test Paper",
            "https://example.com/test",
        )
        paper_id = await conn.fetchval("SELECT id FROM papers WHERE title = 'Test Paper'")

        # Try to insert with legacy status='starred' — should fail CHECK constraint
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, status) VALUES ($1, $2, $3)",
                paper_id,
                999,
                "starred",
            )

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_pulse_rated_then_archived(live_pg_dsn: str) -> None:
    """NEW-M11: paper with pulse_ratings='up' but archived=TRUE must NOT appear in digest.

    Regression guard: before the top-level NOT EXISTS fix the pulse_ratings OR branch
    bypassed the archived/dismissed check, allowing explicitly-archived papers to leak
    into the weekly digest.
    """
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Pulse Rated Archived Paper",
            "https://example.com/pulse-archived",
        )
        paper_id = await conn.fetchval(
            "SELECT id FROM papers WHERE title = 'Pulse Rated Archived Paper'"
        )

        await conn.execute("INSERT INTO topics (name) VALUES ($1)", "Test Topic")
        topic_id = await conn.fetchval("SELECT id FROM topics WHERE name = 'Test Topic'")

        await conn.execute(
            "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, $3)",
            paper_id,
            topic_id,
            0.8,
        )

        # Pulse-rated 'up' (would qualify via the pulse_ratings OR branch)
        await conn.execute(
            "INSERT INTO pulse_ratings (paper_id, user_id, rating) VALUES ($1, $2, $3)",
            paper_id,
            999,
            "up",
        )

        # Archived after rating — user explicitly dismissed interest
        await conn.execute(
            "INSERT INTO paper_user_state (paper_id, user_id, status, archived) "
            "VALUES ($1, $2, $3, $4)",
            paper_id,
            999,
            "new",
            True,
        )

        rows = await _digest_query(conn)
        assert len(rows) == 0, (
            "Archived paper with pulse_ratings='up' must not appear in digest (NEW-M11)"
        )

    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_digest_excludes_pulse_rated_then_dismissed(live_pg_dsn: str) -> None:
    """NEW-M11: paper with pulse_ratings='save' but dismissed=TRUE must NOT appear in digest."""
    import asyncpg

    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await _setup_tables(conn)

        await conn.execute(
            "INSERT INTO papers (title, url, published_date) "
            "VALUES ($1, $2, NOW() - INTERVAL '1 day')",
            "Pulse Rated Dismissed Paper",
            "https://example.com/pulse-dismissed",
        )
        paper_id = await conn.fetchval(
            "SELECT id FROM papers WHERE title = 'Pulse Rated Dismissed Paper'"
        )

        await conn.execute("INSERT INTO topics (name) VALUES ($1)", "Test Topic")
        topic_id = await conn.fetchval("SELECT id FROM topics WHERE name = 'Test Topic'")

        await conn.execute(
            "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, $3)",
            paper_id,
            topic_id,
            0.7,
        )

        # Pulse-rated 'save' (would qualify via the pulse_ratings OR branch)
        await conn.execute(
            "INSERT INTO pulse_ratings (paper_id, user_id, rating) VALUES ($1, $2, $3)",
            paper_id,
            999,
            "save",
        )

        # Dismissed after rating — user explicitly chose to not see it again
        await conn.execute(
            "INSERT INTO paper_user_state (paper_id, user_id, status, dismissed) "
            "VALUES ($1, $2, $3, $4)",
            paper_id,
            999,
            "new",
            True,
        )

        rows = await _digest_query(conn)
        assert len(rows) == 0, (
            "Dismissed paper with pulse_ratings='save' must not appear in digest (NEW-M11)"
        )

    finally:
        await conn.close()
