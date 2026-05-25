"""Baseline schema/constraint invariants — re-homed from the 88-file migration chain.

These tests encode the cross-user-isolation + data-shape invariants that the 88
``db/migrations/*.sql`` per-migration tests used to assert via the *replay* path.
Wave 1 of the full-restructure program squashes the chain into a single
regenerated ``db/init.sql`` baseline; this file re-homes those invariants as
**direct assertions against that regenerated baseline** so the invariants keep
their coverage after the chain (and its per-migration tests) is deleted.

Each invariant below was authored RED-then-GREEN: the assertion was first proven
to FAIL against a constraint-stripped / inverted schema (so it is not vacuous),
then reverted to GREEN against the real regenerated baseline. The per-invariant
red→green observation is recorded in the commit message.

Historical-transform note: several original migration tests asserted *data
backfills* (044 legacy ``status='starred'`` → ``starred=TRUE``; 046 saved
backfill; 077 ``SET NULL`` delete rule; 079 JSONB triple-encode repair). Those
transforms are HISTORICAL — the regenerated baseline embodies only the END
STATE and, by construction, contains no legacy/double-encoded rows to transform.
Per the binding spec we therefore re-home those as the *end-state schema/flag
invariant + a regression guard*, never as a fabricated dead transform, with a
one-line WHY comment at each site.

Live-PG only: gated by ``JARVIS_RUN_LIVE_PG=1`` via the ``live_pg`` marker
(excluded by the default ``addopts``), same convention as
``test_migrations_live.py`` / the per-migration live tests. The regenerated
baseline is applied with ``migration_helpers.apply_fresh_init`` over a
disposable ``postgres:16.8`` container (``live_pg_dsn`` fixture).
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from tests.migration_helpers import apply_fresh_init

pytestmark = pytest.mark.live_pg


async def _seed_paper(conn: asyncpg.Connection, external_id: str) -> int:
    """Insert a minimal valid ``papers`` row, return its id."""
    await conn.execute(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url)
        VALUES ($1, 'arxiv', 'Baseline Invariant', ARRAY['Tester'],
                'https://example.test')
        """,
        external_id,
    )
    return await conn.fetchval("SELECT id FROM papers WHERE external_id = $1", external_id)


# ---------------------------------------------------------------------------
# Invariant 043 — UNIQUE NULLS NOT DISTINCT (cross-user isolation cornerstone)
# Re-homed from: test_migrations_live.py:64-141
#                test_fresh_boot_migration_043_uniqueness_semantics
# Re-home form: constraint-violation INSERT against the baseline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_paper_user_state_unique_nulls_not_distinct(
    live_pg_dsn: str,
) -> None:
    """paper_user_state (paper_id, user_id) is UNIQUE NULLS NOT DISTINCT:
    one per-user row AND one NULL-owner row coexist, but a *second* NULL-owner
    row for the same paper is rejected (NULLs treated as equal)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            paper_id = await _seed_paper(conn, "baseline-043-pus")
            await conn.execute("INSERT INTO users (id, email) VALUES (43, 'b043@example.com')")
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) "
                "VALUES ($1, NULL, 'inbox')",
                paper_id,
            )
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 43, 'inbox')",
                paper_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO paper_user_state (paper_id, user_id, state) "
                    "VALUES ($1, NULL, 'inbox')",
                    paper_id,
                )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_baseline_paper_summaries_unique_nulls_not_distinct(
    live_pg_dsn: str,
) -> None:
    """paper_summaries (paper_id, user_id) is UNIQUE NULLS NOT DISTINCT."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            paper_id = await _seed_paper(conn, "baseline-043-sum")
            await conn.execute("INSERT INTO users (id, email) VALUES (43, 'b043s@example.com')")
            await conn.execute(
                """
                INSERT INTO paper_summaries
                    (paper_id, user_id, summary_brief, summary_detailed, key_findings)
                VALUES ($1, NULL, 'brief', 'detailed', '[]'::jsonb)
                """,
                paper_id,
            )
            await conn.execute(
                """
                INSERT INTO paper_summaries
                    (paper_id, user_id, summary_brief, summary_detailed, key_findings)
                VALUES ($1, 43, 'brief', 'detailed', '[]'::jsonb)
                """,
                paper_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO paper_summaries
                        (paper_id, user_id, summary_brief, summary_detailed, key_findings)
                    VALUES ($1, NULL, 'brief', 'detailed', '[]'::jsonb)
                    """,
                    paper_id,
                )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_baseline_pulse_decks_unique_nulls_not_distinct(
    live_pg_dsn: str,
) -> None:
    """pulse_decks (deck_date, user_id) is UNIQUE NULLS NOT DISTINCT."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users (id, email) VALUES (43, 'b043d@example.com')")
            await conn.execute(
                "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', NULL)"
            )
            await conn.execute(
                "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', 43)"
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', NULL)"
                )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 044 — paper_user_state per-user flags end-state.
# Re-homed from: test_migration_044.py:42-158 (live parts).
# The original `preference VARCHAR CHECK ('none','up','down')` column and the
# legacy `status='starred'` → `starred=TRUE` backfill are HISTORICAL: migration
# 047 DROPPED both `preference` and `status` from paper_user_state, so the
# baseline cannot represent them. The surviving end-state invariant 044
# contributes is the per-user `starred` boolean flag (NOT NULL DEFAULT FALSE).
# Re-home form: end-state schema introspection (no fabricated dead backfill).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_paper_user_state_has_starred_flag_endstate(
    live_pg_dsn: str,
) -> None:
    """paper_user_state.starred is BOOLEAN NOT NULL DEFAULT FALSE, and the
    historical `preference`/`status` columns are absent (dropped by 047)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_name = 'paper_user_state' AND column_name = 'starred'
                """
            )
            assert row is not None, "paper_user_state.starred must exist"
            assert row["data_type"] == "boolean"
            assert row["is_nullable"] == "NO"
            assert row["column_default"] == "false"
            # `preference`/`status` were dropped by migration 047 — the 044
            # preference CHECK + legacy-status backfill are historical, not
            # re-homed (the columns no longer exist to assert against).
            absent = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'paper_user_state'
                   AND column_name IN ('preference', 'status')
                """
            )
            assert absent == [], "legacy 044/046 columns must stay dropped in the baseline"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 046 — paper_user_state updated_at trigger end-state.
# Re-homed from: test_migration_046.py:72-165 (live).
# The original `status CHECK ('new','reading','read')` and the
# `saved`/`dismissed` backfill are HISTORICAL: migration 047 collapsed
# status→state and dropped saved/dismissed. The surviving end-state invariant
# 046 contributes is the row-level updated_at maintenance trigger.
# Re-home form: end-state behavioural assertion (trigger fires on UPDATE).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_paper_user_state_updated_at_trigger_endstate(
    live_pg_dsn: str,
) -> None:
    """The set_updated_at trigger on paper_user_state bumps updated_at on
    UPDATE (the surviving 046 end-state; its status CHECK + saved/dismissed
    backfill are historical, removed by migration 047)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            trigger = await conn.fetchval(
                """
                SELECT tgname FROM pg_trigger
                 WHERE tgrelid = 'paper_user_state'::regclass
                   AND NOT tgisinternal
                   AND tgname = 'set_updated_at_paper_user_state'
                """
            )
            assert trigger == "set_updated_at_paper_user_state", (
                "paper_user_state must carry the set_updated_at trigger"
            )

            paper_id = await _seed_paper(conn, "baseline-046-trig")
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) "
                "VALUES ($1, NULL, 'inbox')",
                paper_id,
            )
            before = await conn.fetchval(
                "SELECT updated_at FROM paper_user_state WHERE paper_id = $1",
                paper_id,
            )
            # Separate autocommit txns → distinct NOW(); sleep guarantees a
            # measurable, non-flaky delta on fast machines.
            await asyncio.sleep(0.05)
            await conn.execute(
                "UPDATE paper_user_state SET state = 'reading' WHERE paper_id = $1",
                paper_id,
            )
            after = await conn.fetchval(
                "SELECT updated_at FROM paper_user_state WHERE paper_id = $1",
                paper_id,
            )
            assert after > before, "set_updated_at trigger must bump updated_at on UPDATE"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 047 — paper_user_state.state ENUM + legacy-cols-absent + trash
# round-trip.
# Re-homed from: test_migration_047.py:88-191 (live).
# Re-home form: schema-introspection (legacy cols absent) +
# constraint-violation INSERT (state CHECK) + small data round-trip
# (trash/restore) on the baseline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_paper_user_state_legacy_columns_absent(
    live_pg_dsn: str,
) -> None:
    """The pre-collapse columns saved/dismissed/archived/status/preference are
    absent; the post-047 state/state_before_trash columns are present."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'paper_user_state'"
                )
            }
        assert "state" in cols
        assert "state_before_trash" in cols
        for legacy in ("saved", "dismissed", "archived", "status", "preference"):
            assert legacy not in cols, f"legacy column {legacy!r} must be dropped"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_baseline_paper_user_state_state_check_rejects_invalid(
    live_pg_dsn: str,
) -> None:
    """paper_user_state.state CHECK admits exactly the 5 lifecycle values;
    an out-of-set value is rejected."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            paper_id = await _seed_paper(conn, "baseline-047-check")
            for idx, valid in enumerate(("inbox", "to_read", "reading", "done", "trash")):
                uid = 470 + idx
                # paper_user_state.user_id FK→users CASCADE: seed a real owner.
                await conn.execute(
                    "INSERT INTO users (id, email) VALUES ($1, $2)",
                    uid,
                    f"b047-{idx}@example.com",
                )
                await conn.execute(
                    "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, $2, $3)",
                    paper_id,
                    uid,
                    valid,
                )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    "INSERT INTO paper_user_state (paper_id, user_id, state) "
                    "VALUES ($1, NULL, 'archived')",
                    paper_id,
                )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_baseline_paper_user_state_trash_restore_round_trip(
    live_pg_dsn: str,
) -> None:
    """Trash records the prior state in state_before_trash; restore returns
    state to it and NULLs state_before_trash."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            paper_id = await _seed_paper(conn, "baseline-047-trash")
            # paper_user_state.user_id FK→users CASCADE: seed a real owner.
            await conn.execute("INSERT INTO users (id, email) VALUES (1, 'b047t@example.com')")
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 1, 'to_read')",
                paper_id,
            )
            # Trash: stash current state, move to 'trash'.
            await conn.execute(
                "UPDATE paper_user_state "
                "SET state_before_trash = state, state = 'trash' "
                "WHERE paper_id = $1 AND user_id = 1",
                paper_id,
            )
            state, before = await conn.fetchrow(
                "SELECT state, state_before_trash FROM paper_user_state "
                "WHERE paper_id = $1 AND user_id = 1",
                paper_id,
            )
            assert state == "trash"
            assert before == "to_read"
            # Restore: state ← state_before_trash; state_before_trash ← NULL.
            await conn.execute(
                "UPDATE paper_user_state "
                "SET state = state_before_trash, state_before_trash = NULL "
                "WHERE paper_id = $1 AND user_id = 1",
                paper_id,
            )
            state, before = await conn.fetchrow(
                "SELECT state, state_before_trash FROM paper_user_state "
                "WHERE paper_id = $1 AND user_id = 1",
                paper_id,
            )
            assert state == "to_read"
            assert before is None
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 077 — every user-owned table has an FK to users(id); papers uses
# `discovered_by` (not `user_id`) and is the canonical-corpus exception.
# Re-homed from: test_migration_077.py:103-200 (live).
# Original 077 asserted delete_rule == 'SET NULL' on 18 tables. That delete
# rule is HISTORICAL: migration 080 flipped 17 of them to CASCADE (079's job is
# 080; asserted there). The surviving 077 invariant is FK *existence* on all 18
# user-owned tables + the papers/discovered_by exception. Re-home form:
# schema-introspection (FK presence + papers exclusion column).
# ---------------------------------------------------------------------------

# The 18 user-owned tables 077 added FK constraints to. `papers` is keyed on
# `discovered_by` (renamed from user_id in mig 072); the rest on `user_id`.
_INV077_USER_ID_TABLES: tuple[str, ...] = (
    "paper_notes",
    "paper_summaries",
    "paper_chunks",
    "paper_user_state",
    "pulse_cards",
    "paper_contradictions",
    "paper_extractions",
    "daily_log",
    "paper_recommendations",
    "projects",
    "tasks",
    "milestones",
    "cards",
    "decks",
    "review_logs",
    "tracked_authors",
    "author_alert_log",
)


@pytest.mark.asyncio
async def test_baseline_user_owned_tables_have_users_fk(live_pg_dsn: str) -> None:
    """All 18 user-owned tables 077 covered have an FK to users(id); papers'
    FK is on the `discovered_by` column (canonical-corpus exception). The
    SET NULL→CASCADE flip is historical (asserted in the 080 invariant)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            for table in _INV077_USER_ID_TABLES:
                row = await conn.fetchrow(
                    """
                    SELECT ccu.table_name AS ref_tbl, ccu.column_name AS ref_col
                      FROM information_schema.table_constraints tc
                      JOIN information_schema.constraint_column_usage ccu
                        ON tc.constraint_name = ccu.constraint_name
                     WHERE tc.constraint_type = 'FOREIGN KEY'
                       AND tc.table_name = $1
                       AND tc.constraint_name = $2
                    """,
                    table,
                    f"{table}_user_id_fkey",
                )
                assert row is not None, f"{table}: missing FK to users(id)"
                assert row["ref_tbl"] == "users"
                assert row["ref_col"] == "id"

            # papers: FK lives on `discovered_by`, NOT `user_id`.
            papers_fk = await conn.fetchrow(
                """
                SELECT kcu.column_name, ccu.table_name AS ref_tbl
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.constraint_type = 'FOREIGN KEY'
                   AND tc.table_name = 'papers'
                   AND tc.constraint_name = 'papers_discovered_by_fkey'
                """
            )
            assert papers_fk is not None, "papers_discovered_by_fkey must exist"
            assert papers_fk["column_name"] == "discovered_by"
            assert papers_fk["ref_tbl"] == "users"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 079 — JSONB columns stay JSONB and round-trip a nested object as
# `object` (regression guard against future re-encoding).
# Re-homed from: test_migration_079.py:107-228 (live, `pg_dsn`).
# The triple-encode REPAIR is HISTORICAL: it converged double-encoded legacy
# rows; the regenerated baseline has none by construction. Per the binding
# spec we do NOT fabricate the dead transform — instead a regression guard:
# the 3 columns are jsonb AND a nested-object insert reads back as
# jsonb_typeof = 'object' (catches a future re-encoding regression).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_jsonb_columns_are_jsonb_and_roundtrip_object(
    live_pg_dsn: str,
) -> None:
    """audit_log.metadata, job_progress.result, job_progress.error are jsonb,
    and a nested object inserted via the asyncpg JSONB codec reads back as a
    JSON object (regression guard — the 079 triple-encode repair is historical
    and has no legacy rows to transform in the baseline)."""
    pool = await asyncpg.create_pool(
        live_pg_dsn,
        min_size=1,
        max_size=2,
        init=lambda c: c.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        ),
    )
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            for table, col in (
                ("audit_log", "metadata"),
                ("job_progress", "result"),
                ("job_progress", "error"),
            ):
                dtype = await conn.fetchval(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = $1 AND column_name = $2",
                    table,
                    col,
                )
                assert dtype == "jsonb", f"{table}.{col} must be jsonb, got {dtype!r}"

            nested = {"outer": {"inner": [1, 2, 3]}}
            await conn.execute(
                "INSERT INTO audit_log (user_id, action, resource, metadata) "
                "VALUES (NULL, 'baseline-079', 'res', $1)",
                nested,
            )
            kind = await conn.fetchval(
                "SELECT jsonb_typeof(metadata) FROM audit_log WHERE action = 'baseline-079'"
            )
            assert kind == "object", (
                f"audit_log.metadata round-trip must stay 'object', got {kind!r} "
                "(a 'string' here = JSONB double-encoding regression)"
            )

            await conn.execute(
                "INSERT INTO job_progress (jarvis_job_id, progress, result, error) "
                "VALUES ('baseline-079', 0.0, $1, $2)",
                nested,
                nested,
            )
            for col in ("result", "error"):
                kind = await conn.fetchval(
                    f"SELECT jsonb_typeof({col}) FROM job_progress "  # noqa: S608
                    "WHERE jarvis_job_id = 'baseline-079'"
                )
                assert kind == "object", (
                    f"job_progress.{col} round-trip must stay 'object', got {kind!r}"
                )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 080 — user-deletion CASCADE on owned data; papers.discovered_by is
# the explicit non-cascade exception (shared-corpus model).
# Re-homed from: test_migration_080.py:37-71 (mock-only today — UPGRADED to a
# real schema test).
# Re-home form: schema-introspection (FK delete_rule == CASCADE × 17 + papers
# exclusion is SET NULL, not CASCADE).
# ---------------------------------------------------------------------------

# The 17 owned-data tables 080 flips to ON DELETE CASCADE — papers is NOT here.
_INV080_CASCADE_TABLES: tuple[str, ...] = (
    "paper_notes",
    "paper_summaries",
    "paper_chunks",
    "paper_user_state",
    "pulse_cards",
    "paper_contradictions",
    "paper_extractions",
    "daily_log",
    "paper_recommendations",
    "projects",
    "tasks",
    "milestones",
    "cards",
    "decks",
    "review_logs",
    "tracked_authors",
)


async def _fk_delete_rule(conn: asyncpg.Connection, table: str, constraint: str) -> str | None:
    return await conn.fetchval(
        """
        SELECT rc.delete_rule
          FROM information_schema.table_constraints tc
          JOIN information_schema.referential_constraints rc
            ON tc.constraint_name = rc.constraint_name
         WHERE tc.constraint_type = 'FOREIGN KEY'
           AND tc.table_name = $1
           AND tc.constraint_name = $2
        """,
        table,
        constraint,
    )


@pytest.mark.asyncio
async def test_baseline_owned_data_cascades_on_user_delete(
    live_pg_dsn: str,
) -> None:
    """All 16 owned-data tables FK users(id) ON DELETE CASCADE; papers'
    discovered_by FK is explicitly SET NULL (shared/system papers survive a
    user deletion under the canonical-corpus model). author_alert_log was
    removed from this set when its user_id FK was flipped to SET NULL by
    the 0091 fold-in (per-user dedupe; rows are not user-owned data)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            for table in _INV080_CASCADE_TABLES:
                rule = await _fk_delete_rule(conn, table, f"{table}_user_id_fkey")
                assert rule == "CASCADE", f"{table}: expected ON DELETE CASCADE, got {rule!r}"
            papers_rule = await _fk_delete_rule(conn, "papers", "papers_discovered_by_fkey")
            assert papers_rule == "SET NULL", (
                "papers.discovered_by must be SET NULL (NOT CASCADE) — a shared "
                f"corpus paper outlives its discoverer; got {papers_rule!r}"
            )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 083 — thread.user_id FK ON DELETE CASCADE.
# Re-homed from: test_migration_083.py:114-121 (live behavioral) +
#                test_migration_083.py:83-98 (FK introspection).
# Re-home form: schema-introspection (delete_rule == CASCADE) +
#               behavioral cascade round-trip (user delete removes thread row).
# Confirmed: db/init.sql:1934 — thread_user_id_fkey … ON DELETE CASCADE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_thread_user_id_fkey_is_cascade(live_pg_dsn: str) -> None:
    """thread_user_id_fkey has delete_rule == CASCADE in the regenerated baseline."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            rule = await _fk_delete_rule(conn, "thread", "thread_user_id_fkey")
            assert rule == "CASCADE", (
                f"thread_user_id_fkey: expected ON DELETE CASCADE, got {rule!r}"
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_baseline_thread_cascades_on_user_delete(live_pg_dsn: str) -> None:
    """Deleting a user removes all their thread rows (ON DELETE CASCADE round-trip).

    Tracks the row by its primary key (not user_id) so the assertion is
    non-vacuous: a SET-NULL FK would leave the row alive with user_id=NULL,
    making count-by-id==1; CASCADE deletes it entirely, making count-by-id==0.
    """
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, role) "
                "VALUES ('baseline-083@example.com', 'user') RETURNING id"
            )
            thread_id = await conn.fetchval(
                "INSERT INTO thread (user_id, title, progress) VALUES ($1, 'kept', 0.5) RETURNING id",
                uid,
            )
            assert await conn.fetchval("SELECT count(*) FROM thread WHERE id = $1", thread_id) == 1
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
            assert (
                await conn.fetchval("SELECT count(*) FROM thread WHERE id = $1", thread_id) == 0
            ), "thread row must cascade-delete when the owning user is removed (not SET NULL)"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 084 — project_questions.user_id FK ON DELETE CASCADE.
# Re-homed from: test_migration_084.py:92-109 (FK introspection) and the
#                implicit behavioral invariant (same I-class as 083).
# Re-home form: schema-introspection (delete_rule == CASCADE for user_id FK) +
#               behavioral cascade round-trip (user delete removes pq row).
# Confirmed: db/init.sql:1885 — project_questions_user_id_fkey … ON DELETE CASCADE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_project_questions_user_id_fkey_is_cascade(live_pg_dsn: str) -> None:
    """project_questions_user_id_fkey has delete_rule == CASCADE in the regenerated baseline."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            rule = await _fk_delete_rule(
                conn, "project_questions", "project_questions_user_id_fkey"
            )
            assert rule == "CASCADE", (
                f"project_questions_user_id_fkey: expected ON DELETE CASCADE, got {rule!r}"
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_baseline_project_questions_cascades_on_user_delete(live_pg_dsn: str) -> None:
    """Deleting a user removes all their project_questions rows (ON DELETE CASCADE round-trip).

    Tracks the row by its primary key (not user_id) so the assertion is
    non-vacuous: project_questions.user_id is NOT NULL, so a SET-NULL FK action
    raises NotNullViolationError rather than silently surviving; CASCADE deletes
    the row cleanly and count-by-id==0.
    """
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users (email, role) "
                "VALUES ('baseline-084@example.com', 'user') RETURNING id"
            )
            proj_id = await conn.fetchval(
                "INSERT INTO projects (user_id, name) VALUES ($1, 'Baseline084Project') RETURNING id",
                uid,
            )
            pq_id = await conn.fetchval(
                "INSERT INTO project_questions (project_id, user_id, body) "
                "VALUES ($1, $2, 'Q?') RETURNING id",
                proj_id,
                uid,
            )
            assert (
                await conn.fetchval("SELECT count(*) FROM project_questions WHERE id = $1", pq_id)
                == 1
            )
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
            assert (
                await conn.fetchval("SELECT count(*) FROM project_questions WHERE id = $1", pq_id)
                == 0
            ), (
                "project_questions row must cascade-delete when the owning user is removed (not SET NULL)"
            )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Invariant 082 — the FK-gap tables: 6 ON DELETE CASCADE, pulse_models SET NULL.
# Re-homed from: test_migration_082.py:120-281 (live).
# Re-home form: schema-introspection (FK delete_rule).
# ---------------------------------------------------------------------------

_INV082_CASCADE_TABLES: tuple[str, ...] = (
    "pulse_decks",
    "recommendation_feedback",
    "source_health",
    "source_run_history",
    "daily_intent",
    "journal_entries",
)
_INV082_SET_NULL_TABLES: tuple[str, ...] = ("pulse_models",)


@pytest.mark.asyncio
async def test_baseline_fk_gap_tables_delete_rules(live_pg_dsn: str) -> None:
    """The 7 FK-gap tables migration 082 closed: 6 CASCADE, pulse_models
    SET NULL (pulse_models NULL user = shared/system model)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            for table in _INV082_CASCADE_TABLES:
                rule = await _fk_delete_rule(conn, table, f"{table}_user_id_fkey")
                assert rule == "CASCADE", f"{table}: expected ON DELETE CASCADE, got {rule!r}"
            for table in _INV082_SET_NULL_TABLES:
                rule = await _fk_delete_rule(conn, table, f"{table}_user_id_fkey")
                assert rule == "SET NULL", f"{table}: expected ON DELETE SET NULL, got {rule!r}"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Migration 0089 — pdf_resolutions table dropped.
# Re-home form: schema-introspection (information_schema.tables absent).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_resolutions_table_dropped(test_db_pool: asyncpg.Pool) -> None:
    """Migration 0089 must drop the pdf_resolutions table.

    test_db_pool applies db/init.sql + run_migrations(), so migration 0089
    (DROP TABLE IF EXISTS pdf_resolutions CASCADE) runs before this assertion.
    """
    async with test_db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'pdf_resolutions'"
        )
    assert row is None, "pdf_resolutions table should be dropped by migration 0089"


# ---------------------------------------------------------------------------
# CFG-MIG-1 — pdf_resolutions absent from both init.sql baseline and live DB.
# Re-home form: schema-introspection via contract_conn (JARVIS_RUN_LIVE_PG=1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_pdf_resolutions_table_absent(contract_conn: asyncpg.Connection) -> None:
    """pdf_resolutions must not exist in public schema.

    Migration 0089 (DROP TABLE IF EXISTS pdf_resolutions CASCADE) removes the
    table from live databases; db/init.sql no longer defines it (CFG-MIG-1).
    This assertion verifies both: if the table reappears it fails here.
    """
    row = await contract_conn.fetchrow(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='pdf_resolutions'"
    )
    assert row is None, (
        "pdf_resolutions table must be absent — migration 0089 dropped it "
        "and db/init.sql no longer defines it (CFG-MIG-1)"
    )


# ---------------------------------------------------------------------------
# Migration 0090 — audit_log append-only rules (folded into init.sql 2026-05-26).
# DELETE and UPDATE on audit_log become silent no-ops; INSERT and TRUNCATE pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_append_only_rules_present(live_pg_dsn: str) -> None:
    """audit_log must carry the DO-INSTEAD-NOTHING rules for DELETE and UPDATE
    (migration 0090, folded into init.sql)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT rulename, ev_type
                  FROM pg_rewrite
                  JOIN pg_class ON pg_rewrite.ev_class = pg_class.oid
                 WHERE pg_class.relname = 'audit_log'
                   AND rulename IN ('no_delete_audit_log', 'no_update_audit_log')
                """,
            )
            names = {r["rulename"] for r in rows}
            assert names == {"no_delete_audit_log", "no_update_audit_log"}, (
                f"audit_log must have both append-only rules, found {names!r}"
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_audit_log_delete_is_silent_noop(live_pg_dsn: str) -> None:
    """DELETE FROM audit_log returns successfully but removes nothing
    (DO INSTEAD NOTHING rule from migration 0090)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log (user_id, action, resource) "
                "VALUES ('u1', 'baseline-0090', 'r1')"
            )
            await conn.execute("DELETE FROM audit_log WHERE action = 'baseline-0090'")
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'baseline-0090'"
            )
            assert remaining == 1, "audit_log DELETE must be a silent no-op"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Migration 0091 — author_alert_log per-user dedupe (folded into init.sql).
# 2-col unique constraint replaced by 3-col unique index;
# user_id FK flipped from CASCADE to SET NULL.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_author_alert_log_three_col_unique_present(live_pg_dsn: str) -> None:
    """author_alert_log must carry the (tracked_author_id, paper_id, user_id)
    unique index (migration 0091, folded into init.sql)."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT indexdef FROM pg_indexes
                 WHERE tablename = 'author_alert_log'
                   AND indexdef LIKE '%(tracked_author_id, paper_id, user_id)%'
                """,
            )
            assert row is not None, (
                "author_alert_log must have a 3-col unique index "
                "(tracked_author_id, paper_id, user_id)"
            )
            assert "UNIQUE" in row["indexdef"], (
                f"3-col index must be UNIQUE, got: {row['indexdef']}"
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_author_alert_log_two_col_unique_absent(live_pg_dsn: str) -> None:
    """The pre-0091 2-col (tracked_author_id, paper_id) unique constraint
    must be gone — keeping it would suppress alerts for other users."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT conname FROM pg_constraint
                 WHERE conrelid = 'public.author_alert_log'::regclass
                   AND contype = 'u'
                   AND conname = 'author_alert_log_tracked_author_id_paper_id_key'
                """,
            )
            assert row is None, f"pre-0091 2-col unique constraint must be absent; got {row!r}"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_author_alert_log_user_id_fk_set_null(live_pg_dsn: str) -> None:
    """author_alert_log.user_id FK delete rule must be SET NULL post-0091:
    a deleted user does not erase the per-user dedupe history."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            rule = await _fk_delete_rule(conn, "author_alert_log", "author_alert_log_user_id_fkey")
            assert rule == "SET NULL", f"author_alert_log.user_id FK must be SET NULL, got {rule!r}"
    finally:
        await pool.close()
