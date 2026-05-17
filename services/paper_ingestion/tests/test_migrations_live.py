"""Live PostgreSQL tests for fresh-init plus migration replay."""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

import asyncpg
import pytest
from paper_ingestion.migrations_runner import run_migrations

pytestmark = pytest.mark.live_pg

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"
_MIGRATIONS_DIR = _REPO_ROOT / "db" / "migrations"
_AUTH_DDL = _MIGRATIONS_DIR / "069_auth.sql"
# Same image the live_pg fixture provisions; reused as a throwaway pg_dump
# client so the host needs no postgresql-client package.
_PG_IMAGE = "postgres:16.8"


def _migration_versions() -> set[int]:
    versions: set[int] = set()
    for sql_file in _MIGRATIONS_DIR.glob("*.sql"):
        try:
            versions.add(int(sql_file.name.split("_", maxsplit=1)[0]))
        except (IndexError, ValueError):
            continue
    return versions


async def _apply_fresh_init(pool: asyncpg.Pool) -> None:
    """Apply the same init.sql that Docker runs for a brand-new database volume."""
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_fresh_init_then_migrations_are_idempotent(live_pg_dsn: str) -> None:
    """Fresh boot path: init.sql, run_migrations(), then run_migrations() again."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            migration_count = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")
            latest_version = await conn.fetchval("SELECT MAX(version) FROM schema_migrations")

        versions = _migration_versions()
        assert migration_count == len(versions)
        assert latest_version == max(versions)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fresh_boot_migration_043_uniqueness_semantics(live_pg_dsn: str) -> None:
    """Migration 043 constraints allow per-user rows and reject duplicate NULL owners."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-043', 'arxiv', 'Live PG 043', ARRAY['Tester'], 'https://example.test')
                """
            )
            paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-043",
            )
            # Post-migration-077 the user_id columns FK to users(id); the
            # per-user rows below need a real owner. NULL-owner rows still
            # exercise migration 043's UNIQUE NULLS NOT DISTINCT semantics.
            await conn.execute(
                "INSERT INTO users (id, email) VALUES (42, 'live-pg-043@example.test')"
            )

            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, NULL, 'inbox')",
                paper_id,
            )
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 42, 'inbox')",
                paper_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, NULL, 'inbox')",
                    paper_id,
                )

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
                VALUES ($1, 42, 'brief', 'detailed', '[]'::jsonb)
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

            await conn.execute(
                "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', NULL)"
            )
            await conn.execute(
                "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', 42)"
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', NULL)"
                )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# init.sql <-> migration-sequence structural drift guard (WS3 / 046-047 class)
# ---------------------------------------------------------------------------
#
# THE DRIFT INVARIANT
# -------------------
# db/init.sql is a HAND-MAINTAINED steady-state snapshot Docker runs once for a
# brand-new database volume. It is NOT generated. The runtime migration runner
# then applies the deliberately-omitted tail migrations.
#
# REPO-HONEST FACT (discovered while building this guard, documented as
# pre-existing structural debt per ROADMAP item 3):
#
#   The migration files are *forward-only deltas on top of init.sql*, NOT a
#   self-contained schema, AND they are NOT replayable from the current
#   snapshot. Migration 001's own header reads "Run this on existing
#   databases"; it does `CREATE INDEX ... ON paper_user_state(status)` — but
#   the CURRENT init.sql snapshot has no `paper_user_state.status` column (a
#   later migration collapsed it to a single-state ENUM). So neither
#   "blank DB + all migrations" NOR "snapshot + replay all migrations" is a
#   buildable path in this repo, by construction. A naive
#   schema(blank+history) vs schema(init.sql) comparison is therefore
#   impossible here — that is the pre-existing debt, not something this guard
#   can or should paper over.
#
# THE CHECKABLE, REPO-HONEST INVARIANT — the DELTA between the hand-curated
# snapshot and the real fresh-install schema is PINNED. The real Docker
# fresh-install path is exactly:
#
#       blank DB -> 069_auth.sql -> init.sql -> run_migrations()
#
# (init.sql pre-seeds schema_migrations to ~v68, so the runner applies only
# the deliberately-omitted tail). The 046/047 bug class is precisely: a
# migration adds/alters a schema object that init.sql does NOT embody, and
# nobody updates init.sql, so a fresh Docker install silently runs on a
# different schema than the snapshot describes. So the guard compares two
# schemas built on the SAME disposable Postgres:
#
#   BASE  = 069_auth.sql -> init.sql            (the hand-curated snapshot)
#   FULL  = 069_auth.sql -> init.sql -> run_migrations()  (real fresh install)
#
# The set difference FULL \ BASE is exactly the schema objects the
# deliberately-omitted tail migrations add. At HEAD that delta is non-empty
# (migrations 33/34/52/53/63-66/69+ are intentionally left out of the
# snapshot) — so "delta must be empty" would be WRONG. Instead the delta is
# pinned to a checked-in golden manifest:
#
#       db/migrations/.init-sql-drift-baseline.txt
#
# A NEW migration that adds/alters an object init.sql does not embody ENLARGES
# the delta -> it no longer matches the golden manifest -> the guard fails
# automatically, printing the exact NEW objects. The author then either
# (a) updates db/init.sql so the snapshot embodies the change (delta shrinks
# back to the manifest), or (b) if the migration is a deliberate
# runtime-only tail addition, regenerates the manifest (a reviewed, explicit
# action) — see db/migrations/README.md 'Drift Guard'. Either way the
# divergence cannot land silently. This is the WS3 / 046-047 drift class,
# caught structurally.
#
# This guard builds BASE and FULL on disposable Postgres, pg_dump
# --schema-only BOTH, normalizes away non-semantic noise, diffs the object
# sets, and asserts the delta equals the pinned manifest.


def _dsn_parts(dsn: str) -> tuple[str, str, str, str]:
    """Split the live_pg DSN into (host, port, password, dbname).

    The fixture yields ``postgresql://jarvis:<pw>@127.0.0.1:<port>/jarvis``.
    """
    u = urllib.parse.urlparse(dsn)
    assert u.hostname and u.port and u.password and u.path, f"unexpected DSN: {dsn}"
    return u.hostname, str(u.port), u.password, u.path.lstrip("/")


def _pg_dump_schema(dsn: str) -> str:
    """Schema-only dump via a throwaway postgres:16.8 client container.

    Run with ``--network host`` so the DSN's ``127.0.0.1:<port>`` (the
    fixture's published port) is reachable without bundling a host
    postgresql-client. ``--rm`` so nothing leaks, mirroring the live_pg
    fixture's disposable-container philosophy.
    """
    host, port, password, dbname = _dsn_parts(dsn)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "-e",
            f"PGPASSWORD={password}",
            _PG_IMAGE,
            "pg_dump",
            "--schema-only",
            "--no-owner",  # ownership is deployment-specific, not schema shape
            "--no-privileges",  # GRANT/REVOKE are environmental, not shape
            "-h",
            host,
            "-p",
            port,
            "-U",
            "jarvis",
            "-d",
            dbname,
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
    )
    return result.stdout


def _normalize_schema(dump: str) -> list[str]:
    """Reduce a pg_dump to a canonical, order-independent set of DEFINITIONS.

    Each strip below is justified as NON-SEMANTIC — it removes noise that can
    differ between two databases that nonetheless have identical table /
    column / type / constraint / index / trigger / function shapes. Anything
    that *is* schema shape is deliberately retained, so a real column drift
    still fails the guard.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for raw in dump.splitlines():
        line = raw.rstrip()

        # Track $$-quoted bodies (PL/pgSQL functions, DO blocks). Inside one,
        # keep every line verbatim — the function body IS schema shape, and a
        # ';' inside it must not be treated as a statement terminator.
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar

        if not in_dollar:
            # -- comments and pg_dump banner/section comments: cosmetic only.
            if line.lstrip().startswith("--") or not line.strip():
                continue
            # SET / SELECT pg_catalog.set_config: session GUCs (search_path,
            # client_encoding, statement_timeout...). Not schema shape.
            if re.match(r"^\s*SET\s", line) or "pg_catalog.set_config" in line:
                continue
            # ALTER ... OWNER TO: ownership is deployment-specific.
            if re.search(r"\bOWNER TO\b", line):
                continue
            # COMMENT ON EXTENSION: extension provenance text, not shape.
            if re.match(r"^\s*COMMENT ON EXTENSION\b", line):
                continue
            # COMMENT ON SCHEMA public: the public namespace's comment is
            # never table/column shape. It is also asymmetric by harness
            # artifact: this test does `DROP SCHEMA public CASCADE; CREATE
            # SCHEMA public;` to reset between the two build passes, which
            # clears Postgres' default comment, so pg_dump then emits an
            # explicit `COMMENT ON SCHEMA public IS ''` on the reset path but
            # not on the fresh-container path. An (empty) schema comment is
            # semantically nothing — strip it on both sides.
            if re.match(r"^\s*COMMENT ON SCHEMA\b", line):
                continue
            # CREATE EXTENSION ... the extension's installed VERSION can vary
            # by image patch level; the extension's PRESENCE is shape, its
            # version string is not. Canonicalize the version away.
            line = re.sub(r"(CREATE EXTENSION[^;]*?)\s+WITH SCHEMA \w+", r"\1", line)

        buf.append(line)
        if not in_dollar and line.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))

    canonical: list[str] = []
    for stmt in statements:
        s = stmt.strip()

        # The schema_migrations DATA rows differ BY DESIGN: Path A inserts one
        # row per migration via run_migrations; Path B's init.sql pre-seeds
        # ~v68 then the runner inserts the tail. Same final set, different
        # applied_at timestamps and INSERT grouping. The TABLE definition is
        # shape and is kept; only its data INSERTs are dropped.
        if re.match(r"^INSERT INTO\s+(public\.)?schema_migrations\b", s, re.IGNORECASE):
            continue
        # Sequence restart / OWNED BY: current sequence value is runtime
        # state, not schema shape. Keep CREATE SEQUENCE structure; drop the
        # volatile pieces.
        if re.match(r"^SELECT pg_catalog.setval\b", s, re.IGNORECASE):
            continue
        if re.match(r"^ALTER SEQUENCE\b.*\bOWNED BY\b", s, re.IGNORECASE | re.DOTALL):
            continue

        # Collapse internal whitespace so reindentation between the two dump
        # passes is not mistaken for drift. Identifiers/keywords are
        # preserved; only run-length of spaces/newlines is normalized.
        s = re.sub(r"\s+", " ", s).strip()
        canonical.append(s)

    # pg_dump emits objects in dependency/OID order, which can differ between
    # the two build paths even when the object SET is identical. Sorting makes
    # the comparison order-independent — we assert the same DEFINITIONS exist,
    # not the same emission order.
    return sorted(canonical)


_DRIFT_BASELINE = _MIGRATIONS_DIR / ".init-sql-drift-baseline.txt"


async def _apply_init_base(pool: asyncpg.Pool) -> None:
    """Apply the only schema BASE that exists in this repo.

    Mirrors the ``two_users`` fixture EXACTLY: init.sql FK-references
    ``users(id)`` but never creates ``users``/``sessions`` (those live only in
    migration 069), so apply the idempotent 069 auth DDL first, then init.sql
    whole.
    """
    async with pool.acquire() as conn:
        await conn.execute(_AUTH_DDL.read_text(encoding="utf-8"))
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))


def _read_baseline() -> list[str]:
    """The pinned set of objects the deliberately-omitted tail migrations add.

    Each non-comment line is one normalized schema-object definition. ``#``
    lines are documentation. Stored sorted so a regeneration produces a
    minimal, reviewable diff.
    """
    lines: list[str] = []
    for raw in _DRIFT_BASELINE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        lines.append(raw)
    return sorted(lines)


_BASELINE_HEADER = (
    "# GOLDEN MANIFEST — init.sql <-> migration-sequence drift baseline\n"
    "# =============================================================\n"
    "# This is the PINNED set of schema objects that the deliberately-\n"
    "# omitted tail migrations add on top of the db/init.sql snapshot\n"
    "# (FULL \\\\ BASE, see test_init_sql_matches_migration_sequence).\n"
    "#\n"
    "# DO NOT hand-edit. Regenerate ONLY as a deliberate, reviewed action\n"
    "# when a migration is intentionally a runtime-only tail addition\n"
    "# (JARVIS_REGEN_DRIFT_BASELINE=1; see db/migrations/README.md\n"
    "# 'Drift Guard'). For a normal schema change, update db/init.sql\n"
    "# instead so the snapshot embodies it.\n"
)


def _write_baseline(delta: list[str]) -> None:
    """Persist the pinned manifest (header + sorted object set)."""
    _DRIFT_BASELINE.write_text(
        f"{_BASELINE_HEADER}# Objects: {len(delta)}\n" + "\n".join(delta) + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_init_sql_matches_migration_sequence(live_pg_dsn: str) -> None:
    """STRUCTURAL guard: the init.sql<->migration delta is PINNED.

    BASE = 069_auth.sql + init.sql (the hand-curated snapshot).
    FULL = BASE + run_migrations() (the real Docker fresh-install path).

    ``FULL \\ BASE`` is exactly the schema objects the deliberately-omitted
    tail migrations add. That set is pinned to
    ``db/migrations/.init-sql-drift-baseline.txt``. A NEW migration that
    adds/alters an object init.sql does not embody enlarges the delta -> it
    no longer matches the manifest -> this fails automatically, printing the
    exact NEW objects so the author either updates db/init.sql or (a reviewed,
    explicit action) regenerates the manifest. See db/migrations/README.md
    'Drift Guard'.

    NOTE: a "blank DB + full migration history" path is impossible in this
    repo by construction (migrations are forward-only deltas on the init.sql
    base and are NOT replayable from the current snapshot — pre-existing
    structural debt, ROADMAP item 3). Pinning the snapshot<->fresh-install
    delta is the repo-honest structural check that still catches the 046/047
    drift class automatically.
    """
    # BASE: hand-curated snapshot only.
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_init_base(pool)
        base = set(_normalize_schema(_pg_dump_schema(live_pg_dsn)))
    finally:
        await pool.close()

    # Reset to a truly blank DB so FULL is built from the identical baseline.
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await pool.close()

    # FULL: the real fresh-install path (init.sql pre-seed makes run_migrations
    # apply only the deliberately-omitted tail).
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_init_base(pool)
        await run_migrations(pool)
        full = set(_normalize_schema(_pg_dump_schema(live_pg_dsn)))
    finally:
        await pool.close()

    delta = sorted(full - base)

    # Opt-in regeneration: a deliberate, reviewed action documented in
    # db/migrations/README.md for genuine runtime-only tail additions. Rewrites
    # the manifest from the live delta and passes, so the author commits the
    # new pinned objects alongside the migration.
    if os.environ.get("JARVIS_REGEN_DRIFT_BASELINE") == "1":
        _write_baseline(delta)
        return

    baseline = _read_baseline()

    if delta != baseline:
        added = sorted(set(delta) - set(baseline))  # objects NOT yet pinned
        removed = sorted(set(baseline) - set(delta))  # pinned but now gone
        diff = "\n".join(
            difflib.unified_diff(
                baseline,
                delta,
                fromfile="db/migrations/.init-sql-drift-baseline.txt (pinned)",
                tofile="actual FULL \\ BASE delta at HEAD",
                lineterm="",
            )
        )
        pytest.fail(
            "db/init.sql has DRIFTED from the migration sequence.\n\n"
            "The set of schema objects the tail migrations add on top of the "
            "init.sql snapshot no longer matches the pinned baseline. Either a "
            "new migration added/altered an object init.sql does not embody "
            "(update db/init.sql), or this is a deliberate runtime-only tail "
            "addition (regenerate the baseline — a reviewed action). See "
            "db/migrations/README.md 'Drift Guard'.\n\n"
            f"NEW objects not in baseline ({len(added)}):\n"
            + ("\n".join(f"  + {o}" for o in added) or "  (none)")
            + f"\n\nBaseline objects no longer present ({len(removed)}):\n"
            + ("\n".join(f"  - {o}" for o in removed) or "  (none)")
            + f"\n\n--- unified diff ---\n{diff}"
        )


@pytest.mark.asyncio
async def test_false_applied_rows_are_repaired_and_replayed(live_pg_dsn: str) -> None:
    """Old init.sql snapshots may have marked migrations applied without schema evidence."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)

        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                [(33,), (52,), (54,)],
            )

        await run_migrations(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            encrypted_value_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'user_config'
                      AND column_name = 'encrypted_value'
                )
                """
            )
            procrastinate_exists = await conn.fetchval(
                """
                SELECT
                    to_regtype('public.procrastinate_job_to_defer_v1') IS NOT NULL
                    AND to_regclass('public.procrastinate_jobs') IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM pg_proc
                        WHERE proname = 'procrastinate_defer_jobs_v1'
                    )
                """
            )
            job_progress_exists = await conn.fetchval(
                "SELECT to_regclass('public.job_progress') IS NOT NULL"
            )
            applied_versions = {
                row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")
            }

        versions = _migration_versions()
        assert encrypted_value_exists is True
        assert procrastinate_exists is True
        assert job_progress_exists is True
        assert applied_versions == versions
    finally:
        await pool.close()
