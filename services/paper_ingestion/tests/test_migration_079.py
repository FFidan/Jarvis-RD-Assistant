"""Tests for migration 079 — JSONB repair for audit_log + job_progress.

Migration 079 extends the converging-loop UPDATE pattern from migration 075
to three additional JSONB columns that were missed:

  audit_log.metadata    (migration 030)
  job_progress.result   (migration 058)
  job_progress.error    (migration 058)

Text-based tests always run; live-PG tests require JARVIS_RUN_LIVE_PG=1.
"""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/079_jsonb_repair_additional.sql"


# ---------------------------------------------------------------------------
# Text-based assertions — always run (no DB dependency).
# ---------------------------------------------------------------------------


def test_migration_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_covers_audit_log_metadata() -> None:
    sql = MIGRATION.read_text()
    assert "audit_log" in sql
    assert "metadata" in sql


def test_migration_covers_job_progress_result() -> None:
    sql = MIGRATION.read_text()
    assert "job_progress" in sql
    assert "result" in sql


def test_migration_covers_job_progress_error() -> None:
    sql = MIGRATION.read_text()
    assert "job_progress" in sql
    assert "error" in sql


def test_migration_uses_converging_loop_pattern() -> None:
    """Migration must use iterative DO-block with EXIT condition (same as 075)."""
    sql = MIGRATION.read_text()
    assert "LOOP" in sql
    assert "EXIT WHEN" in sql
    assert "_rows_updated = 0" in sql
    # Safety cap
    assert "_pass >= 10" in sql


def test_migration_uses_jsonb_typeof_predicate() -> None:
    sql = MIGRATION.read_text()
    assert "jsonb_typeof" in sql
    assert "'string'" in sql


def test_migration_has_three_do_blocks() -> None:
    """One DO block per column (audit_log.metadata, job_progress.result, job_progress.error)."""
    sql = MIGRATION.read_text()
    assert sql.count("DO $$") == 3, f"Expected 3 DO $$ blocks, found {sql.count('DO $$')}"


def test_no_begin_commit_in_migration() -> None:
    """Runner wraps migration in a savepoint; SQL-transaction BEGIN/COMMIT must not appear.

    PL/pgSQL DO-block bodies use ``BEGIN`` as a block delimiter (not a
    transaction statement) — those are allowed.  Only top-level ``BEGIN;``
    and ``COMMIT;`` SQL transaction statements are forbidden.
    """
    sql = MIGRATION.read_text()
    # Only match lines that are *solely* BEGIN; or COMMIT; (SQL tx statements).
    # PL/pgSQL block-begin lines look like "    BEGIN" (no semicolon, indented).
    lines = [ln.strip().upper() for ln in sql.splitlines() if ln.strip()]
    bare_begin = any(ln == "BEGIN;" for ln in lines)
    bare_commit = any(ln in ("COMMIT;", "COMMIT") for ln in lines)
    assert not bare_begin, "Migration must not include bare BEGIN; (runner handles tx)"
    assert not bare_commit, "Migration must not include bare COMMIT (runner handles tx)"


# ---------------------------------------------------------------------------
# Live-PG tests — require JARVIS_RUN_LIVE_PG=1 and a running Postgres instance.
# ---------------------------------------------------------------------------


def _triple_encode(value: dict) -> str:
    """Simulate triple-encoding: json.dumps(json.dumps(json.dumps(value))).

    The first dumps produces a JSON string of the dict.
    The second wraps it in another JSON string.
    The third wraps again.
    asyncpg's JSONB codec applies one more encode on store,
    so what ends up in Postgres is quadruple-encoded — but
    ``jsonb_typeof`` still returns 'string' for any double+ encoding.
    For test purposes we inject a single extra-encoded value directly
    via a raw SQL cast so the DB sees jsonb_typeof = 'string'.
    """
    return json.dumps(json.dumps(value))


@pytest.mark.live_pg
async def test_audit_log_metadata_repaired(pg_dsn: str) -> None:
    """Triple-encoded audit_log.metadata rows are unwrapped by migration 079."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        # Seed a double-encoded row: store a JSON *string* literal as the JSONB value
        # by casting a text literal directly — bypasses the asyncpg JSONB codec.
        raw_double = json.dumps({"key": "value"})  # '{"key": "value"}'
        await conn.execute(  # nolint:jsonb-double-encode — intentional seed for repair test
            "INSERT INTO audit_log (user_id, action, resource, metadata) "
            "VALUES ($1, $2, $3, $4::text::jsonb)",
            None,
            "test_action",
            "test_resource",
            json.dumps(raw_double),  # outer string → jsonb_typeof = 'string'
        )
        row_id = await conn.fetchval(
            "SELECT id FROM audit_log WHERE action = 'test_action' ORDER BY id DESC LIMIT 1"
        )

        # Verify the seed is actually double-encoded
        kind_before = await conn.fetchval(
            "SELECT jsonb_typeof(metadata) FROM audit_log WHERE id = $1", row_id
        )
        assert kind_before == "string", f"Seed failed: expected 'string', got {kind_before!r}"

        # Run the migration SQL
        migration_sql = MIGRATION.read_text()
        await conn.execute(migration_sql)

        # After migration, metadata should be a JSON object
        kind_after = await conn.fetchval(
            "SELECT jsonb_typeof(metadata) FROM audit_log WHERE id = $1", row_id
        )
        assert kind_after == "object", f"Expected 'object' after repair, got {kind_after!r}"
    finally:
        await conn.execute("DELETE FROM audit_log WHERE action = 'test_action'")
        await conn.close()


@pytest.mark.live_pg
async def test_job_progress_result_repaired(pg_dsn: str) -> None:
    """Triple-encoded job_progress.result rows are unwrapped by migration 079."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        raw_double = json.dumps({"status": "done"})
        job_id = "test-migration-079-result"
        await conn.execute(  # nolint:jsonb-double-encode — intentional seed for repair test
            "INSERT INTO job_progress (jarvis_job_id, progress, result) "
            "VALUES ($1, $2, $3::text::jsonb)",
            job_id,
            0.0,
            json.dumps(raw_double),
        )

        kind_before = await conn.fetchval(
            "SELECT jsonb_typeof(result) FROM job_progress WHERE jarvis_job_id = $1", job_id
        )
        assert kind_before == "string"

        await conn.execute(MIGRATION.read_text())

        kind_after = await conn.fetchval(
            "SELECT jsonb_typeof(result) FROM job_progress WHERE jarvis_job_id = $1", job_id
        )
        assert kind_after == "object", f"Expected 'object' after repair, got {kind_after!r}"
    finally:
        await conn.execute("DELETE FROM job_progress WHERE jarvis_job_id = $1", job_id)
        await conn.close()


@pytest.mark.live_pg
async def test_job_progress_error_repaired(pg_dsn: str) -> None:
    """Triple-encoded job_progress.error rows are unwrapped by migration 079."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        raw_double = json.dumps({"message": "boom"})
        job_id = "test-migration-079-error"
        await conn.execute(  # nolint:jsonb-double-encode — intentional seed for repair test
            "INSERT INTO job_progress (jarvis_job_id, progress, error) "
            "VALUES ($1, $2, $3::text::jsonb)",
            job_id,
            0.0,
            json.dumps(raw_double),
        )

        kind_before = await conn.fetchval(
            "SELECT jsonb_typeof(error) FROM job_progress WHERE jarvis_job_id = $1", job_id
        )
        assert kind_before == "string"

        await conn.execute(MIGRATION.read_text())

        kind_after = await conn.fetchval(
            "SELECT jsonb_typeof(error) FROM job_progress WHERE jarvis_job_id = $1", job_id
        )
        assert kind_after == "object", f"Expected 'object' after repair, got {kind_after!r}"
    finally:
        await conn.execute("DELETE FROM job_progress WHERE jarvis_job_id = $1", job_id)
        await conn.close()


@pytest.mark.live_pg
async def test_null_job_progress_columns_untouched(pg_dsn: str) -> None:
    """NULL result/error rows are skipped without error."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        job_id = "test-migration-079-nulls"
        await conn.execute(
            "INSERT INTO job_progress (jarvis_job_id, progress) VALUES ($1, $2)",
            job_id,
            0.0,
        )
        # Should complete without raising
        await conn.execute(MIGRATION.read_text())
        row = await conn.fetchrow(
            "SELECT result, error FROM job_progress WHERE jarvis_job_id = $1", job_id
        )
        assert row["result"] is None
        assert row["error"] is None
    finally:
        await conn.execute("DELETE FROM job_progress WHERE jarvis_job_id = $1", job_id)
        await conn.close()
