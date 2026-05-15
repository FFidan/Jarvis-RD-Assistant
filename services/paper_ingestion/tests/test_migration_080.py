"""Structural tests for migration 080 (user-deletion cascade FKs).

Migration 080 flips the 17 owned-data FKs from ON DELETE SET NULL to ON
DELETE CASCADE and refuses to run while any covered table holds NULL-user
rows. ``papers`` is deliberately excluded — a NULL ``papers.discovered_by``
is a shared/system paper under the canonical-corpus model, not an orphan.
"""

from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/080_user_deletion_cascade.sql"

# The 17 cascade tables — papers is intentionally NOT in this list.
CASCADE_TABLES: tuple[str, ...] = (
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


def test_migration_080_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_080_cascades_all_17_tables() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in CASCADE_TABLES:
        assert f"ADD CONSTRAINT {table}_user_id_fkey" in sql, f"missing ADD CONSTRAINT for {table}"
        assert f"DROP CONSTRAINT IF EXISTS {table}_user_id_fkey" in sql, (
            f"missing idempotent DROP for {table}"
        )
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert executable.count("REFERENCES users(id) ON DELETE CASCADE") == len(CASCADE_TABLES)
    assert executable.count("EXCEPTION WHEN duplicate_object THEN NULL;") == len(CASCADE_TABLES)


def test_migration_080_excludes_papers_discovered_by() -> None:
    """papers must NOT be cascade-deleted or NULL-checked (shared corpus)."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ADD CONSTRAINT papers_discovered_by_fkey" not in sql
    assert "papers_discovered_by_fkey" not in sql
    # papers must not appear in the pre-flight cascade_tables array.
    array_block = sql.split("cascade_tables TEXT[] := ARRAY[")[1].split("]")[0]
    assert "'papers'" not in array_block


def test_migration_080_refuses_on_null_user_rows() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "RAISE EXCEPTION" in sql
    assert "scripts/migrate_null_user_data.py" in sql
    assert "WHERE user_id IS NULL" in sql


def test_migration_080_no_outer_transaction() -> None:
    """Runner wraps each migration; migration must not BEGIN/COMMIT itself."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for line in sql.splitlines():
        stripped = line.strip().upper()
        assert not stripped.startswith(("BEGIN;", "COMMIT;", "ROLLBACK;"))
