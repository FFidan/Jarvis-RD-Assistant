"""Tests for DB migration 043 (multi-user unique constraint fixup + pulse user_id).

No live PostgreSQL is available in the host test environment.  Strategy:

1. Assert the SQL file exists.
2. Assert it contains the required DDL via regex — structural contract tests.
3. Assert idempotence guards (DROP CONSTRAINT IF EXISTS, ADD COLUMN IF NOT EXISTS,
   CREATE INDEX IF NOT EXISTS).
4. Assert UNIQUE NULLS NOT DISTINCT is used (PostgreSQL 15+ syntax).

The semantic test (two users with the same paper_id do NOT conflict) is described
as a docstring; it requires a live DB and runs in Docker integration tests.
"""

import re
from pathlib import Path

import pytest

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"
_MIGRATION_FILE = _MIGRATIONS_DIR / "043_multiuser_unique_constraints.sql"


@pytest.fixture(scope="module")
def sql_text() -> str:
    assert _MIGRATION_FILE.exists(), (
        f"Migration file not found: {_MIGRATION_FILE}. "
        "Create db/migrations/043_multiuser_unique_constraints.sql first."
    )
    return _MIGRATION_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_migration_file_exists():
    """Migration 043 SQL file must exist."""
    assert _MIGRATION_FILE.exists(), f"Missing: {_MIGRATION_FILE}"


# ---------------------------------------------------------------------------
# paper_user_state constraints
# ---------------------------------------------------------------------------


def test_drops_paper_user_state_single_paper_key(sql_text):
    """Must DROP the auto-named single-paper unique constraint."""
    assert re.search(
        r"ALTER TABLE paper_user_state\s+DROP CONSTRAINT IF EXISTS paper_user_state_paper_id_key",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing DROP CONSTRAINT IF EXISTS paper_user_state_paper_id_key"


def test_adds_paper_user_state_composite_unique(sql_text):
    """Must add UNIQUE NULLS NOT DISTINCT (paper_id, user_id) to paper_user_state."""
    assert re.search(
        r"ALTER TABLE paper_user_state.*?UNIQUE NULLS NOT DISTINCT\s*\(paper_id,\s*user_id\)",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing UNIQUE NULLS NOT DISTINCT (paper_id, user_id) on paper_user_state"


# ---------------------------------------------------------------------------
# paper_summaries constraints
# ---------------------------------------------------------------------------


def test_drops_paper_summaries_single_paper_key(sql_text):
    """Must DROP the auto-named single-paper unique constraint on paper_summaries."""
    assert re.search(
        r"ALTER TABLE paper_summaries\s+DROP CONSTRAINT IF EXISTS paper_summaries_paper_id_key",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing DROP CONSTRAINT IF EXISTS paper_summaries_paper_id_key"


def test_adds_paper_summaries_composite_unique(sql_text):
    """Must add UNIQUE NULLS NOT DISTINCT (paper_id, user_id) to paper_summaries."""
    assert re.search(
        r"ALTER TABLE paper_summaries.*?UNIQUE NULLS NOT DISTINCT\s*\(paper_id,\s*user_id\)",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing UNIQUE NULLS NOT DISTINCT (paper_id, user_id) on paper_summaries"


# ---------------------------------------------------------------------------
# pulse_decks user_id column (C2 doc)
# ---------------------------------------------------------------------------


def test_pulse_decks_user_id_column_added(sql_text):
    """Must add nullable user_id to pulse_decks with IF NOT EXISTS."""
    assert re.search(
        r"ALTER TABLE pulse_decks\s+ADD COLUMN IF NOT EXISTS user_id INTEGER NULL",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing ADD COLUMN IF NOT EXISTS user_id INTEGER NULL on pulse_decks"


def test_pulse_decks_drops_single_date_key(sql_text):
    """Must drop old UNIQUE(deck_date) constraint from pulse_decks."""
    assert re.search(
        r"ALTER TABLE pulse_decks\s+DROP CONSTRAINT IF EXISTS pulse_decks_deck_date_key",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing DROP CONSTRAINT IF EXISTS pulse_decks_deck_date_key"


def test_pulse_decks_composite_unique(sql_text):
    """Must add UNIQUE NULLS NOT DISTINCT (deck_date, user_id) to pulse_decks."""
    assert re.search(
        r"ALTER TABLE pulse_decks.*?UNIQUE NULLS NOT DISTINCT\s*\(deck_date,\s*user_id\)",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing UNIQUE NULLS NOT DISTINCT (deck_date, user_id) on pulse_decks"


# ---------------------------------------------------------------------------
# pulse_cards user_id column (C2 doc)
# ---------------------------------------------------------------------------


def test_pulse_cards_user_id_column_added(sql_text):
    """Must add nullable user_id to pulse_cards with IF NOT EXISTS."""
    assert re.search(
        r"ALTER TABLE pulse_cards\s+ADD COLUMN IF NOT EXISTS user_id INTEGER NULL",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing ADD COLUMN IF NOT EXISTS user_id INTEGER NULL on pulse_cards"


# ---------------------------------------------------------------------------
# Idempotence: IF EXISTS / IF NOT EXISTS guards
# ---------------------------------------------------------------------------


def test_no_bare_drop_constraint(sql_text):
    """All DROP CONSTRAINT must use IF EXISTS."""
    bare = re.findall(r"DROP CONSTRAINT\s+(?!IF EXISTS)\w+", sql_text, re.IGNORECASE)
    assert not bare, f"Bare DROP CONSTRAINT without IF EXISTS: {bare}"


def test_no_bare_add_column(sql_text):
    """All ADD COLUMN must use IF NOT EXISTS."""
    bare = re.findall(r"ADD COLUMN\s+(?!IF NOT EXISTS)\w+", sql_text, re.IGNORECASE)
    assert not bare, f"Bare ADD COLUMN without IF NOT EXISTS: {bare}"


def test_no_bare_create_index(sql_text):
    """All CREATE INDEX must use IF NOT EXISTS."""
    bare = re.findall(r"CREATE INDEX\s+(?!IF NOT EXISTS)\w+", sql_text, re.IGNORECASE)
    assert not bare, f"Bare CREATE INDEX without IF NOT EXISTS: {bare}"


# ---------------------------------------------------------------------------
# Semantic contract (live-DB-only note)
# ---------------------------------------------------------------------------


def test_migration_043_allows_two_users_same_paper_user_state():
    """SEMANTIC CONTRACT (host mock only): after migration 043, inserting the same
    paper_id with two different user_id values must NOT violate the unique constraint.

    This is validated at the SQL-structure level above:
    - The old UNIQUE(paper_id) constraint is dropped.
    - A new UNIQUE NULLS NOT DISTINCT (paper_id, user_id) is added.

    With two rows (paper_id=1, user_id=1) and (paper_id=1, user_id=2), the
    composite key differs so no conflict is raised.  The live integration test
    runs this inside Docker where a real PostgreSQL 16 instance is available.
    """
    # Structural assertion: composite constraint replaces the per-paper one.
    sql = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "paper_user_state_paper_id_key" in sql, "Old constraint name not referenced"
    assert "UNIQUE NULLS NOT DISTINCT (paper_id, user_id)" in sql or re.search(
        r"UNIQUE NULLS NOT DISTINCT\s*\(paper_id,\s*user_id\)", sql
    ), "Composite UNIQUE NULLS NOT DISTINCT not found"
