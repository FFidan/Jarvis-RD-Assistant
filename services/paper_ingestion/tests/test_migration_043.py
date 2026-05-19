"""Tests for DB migration 043 (multi-user unique constraint fixup + pulse user_id).

No live PostgreSQL is available in the host test environment.  Strategy:

1. Assert the SQL file exists.
2. Assert it contains the required DDL via regex — structural contract tests.
3. Assert defensive PL/pgSQL blocks introspect pg_constraint for any UNIQUE
   covering the legacy single column, regardless of auto-generated name (H5 fix).
4. Assert idempotence guards (ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
5. Assert UNIQUE NULLS NOT DISTINCT is used (PostgreSQL 15+ syntax).

The semantic test (two users with the same paper_id do NOT conflict) is described
as a docstring; it requires a live DB and runs in Docker integration tests.
Live-fixture migration test (pytest-postgresql) is deferred to a future sprint.
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
# paper_user_state constraints
# ---------------------------------------------------------------------------


def test_drops_paper_user_state_single_paper_key(sql_text):
    """Must use a defensive PL/pgSQL block to drop any UNIQUE covering only paper_id.

    The block must introspect pg_constraint so it works regardless of the
    auto-generated constraint name (H5 fix — diverged constraint names).
    """
    assert "pg_constraint" in sql_text, "Missing pg_constraint introspection block"
    assert re.search(
        r"con\.contype\s*=\s*'u'",
        sql_text,
        re.IGNORECASE,
    ), "Missing con.contype = 'u' filter in PL/pgSQL block"
    assert re.search(
        r"rel\.relname\s*=\s*'paper_user_state'",
        sql_text,
        re.IGNORECASE,
    ), "Missing rel.relname = 'paper_user_state' filter in PL/pgSQL block"
    assert re.search(
        r"ARRAY\['paper_id'\]",
        sql_text,
        re.IGNORECASE,
    ), "Missing ARRAY['paper_id'] column filter for paper_user_state block"


def test_adds_paper_user_state_composite_unique(sql_text):
    """Must add UNIQUE NULLS NOT DISTINCT (paper_id, user_id) to paper_user_state."""
    assert re.search(
        r"ALTER TABLE paper_user_state.*?UNIQUE NULLS NOT DISTINCT\s*\(paper_id,\s*user_id\)",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing UNIQUE NULLS NOT DISTINCT (paper_id, user_id) on paper_user_state"
    assert "paper_user_state_paper_id_user_id_key" in sql_text


def test_paper_user_state_add_guard_is_defensive(sql_text):
    # Sprint 7 B9: the ADD-constraint guard must introspect pg_constraint by
    # column composition, not by hardcoded constraint name. Otherwise a
    # deployment with a non-default name would dodge the guard and the ADD
    # would attempt to create a duplicate.
    assert re.search(
        r"ARRAY\['paper_id',\s*'user_id'\]",
        sql_text,
    ), "ADD guard must detect (paper_id, user_id) by column array, not by name"


# ---------------------------------------------------------------------------
# paper_summaries constraints
# ---------------------------------------------------------------------------


def test_drops_paper_summaries_single_paper_key(sql_text):
    """Must use a defensive PL/pgSQL block to drop any UNIQUE covering only paper_id
    on paper_summaries, regardless of auto-generated constraint name (H5 fix).
    """
    assert re.search(
        r"rel\.relname\s*=\s*'paper_summaries'",
        sql_text,
        re.IGNORECASE,
    ), "Missing rel.relname = 'paper_summaries' filter in PL/pgSQL block"
    assert re.search(
        r"ALTER TABLE paper_summaries DROP CONSTRAINT",
        sql_text,
        re.IGNORECASE,
    ), "Missing EXECUTE DROP CONSTRAINT for paper_summaries in PL/pgSQL block"


def test_adds_paper_summaries_composite_unique(sql_text):
    """Must add UNIQUE NULLS NOT DISTINCT (paper_id, user_id) to paper_summaries."""
    assert re.search(
        r"ALTER TABLE paper_summaries.*?UNIQUE NULLS NOT DISTINCT\s*\(paper_id,\s*user_id\)",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing UNIQUE NULLS NOT DISTINCT (paper_id, user_id) on paper_summaries"
    assert "paper_summaries_paper_id_user_id_key" in sql_text


def test_pulse_decks_add_guard_is_defensive(sql_text):
    # Sprint 7 B9: pulse_decks ADD guard must introspect (deck_date, user_id)
    # by column composition.
    assert re.search(
        r"ARRAY\['deck_date',\s*'user_id'\]",
        sql_text,
    ), "pulse_decks ADD guard must detect (deck_date, user_id) by column array"


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
    """Must use a defensive PL/pgSQL block to drop any UNIQUE covering only deck_date
    on pulse_decks, regardless of auto-generated constraint name (H5 fix).
    """
    assert re.search(
        r"rel\.relname\s*=\s*'pulse_decks'",
        sql_text,
        re.IGNORECASE,
    ), "Missing rel.relname = 'pulse_decks' filter in PL/pgSQL block"
    assert re.search(
        r"ARRAY\['deck_date'\]",
        sql_text,
        re.IGNORECASE,
    ), "Missing ARRAY['deck_date'] column filter for pulse_decks block"
    assert re.search(
        r"ALTER TABLE pulse_decks DROP CONSTRAINT",
        sql_text,
        re.IGNORECASE,
    ), "Missing EXECUTE DROP CONSTRAINT for pulse_decks in PL/pgSQL block"


def test_pulse_decks_composite_unique(sql_text):
    """Must add UNIQUE NULLS NOT DISTINCT (deck_date, user_id) to pulse_decks."""
    assert re.search(
        r"ALTER TABLE pulse_decks.*?UNIQUE NULLS NOT DISTINCT\s*\(deck_date,\s*user_id\)",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    ), "Missing UNIQUE NULLS NOT DISTINCT (deck_date, user_id) on pulse_decks"
    assert "pulse_decks_deck_date_user_id_key" in sql_text


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


def test_composite_constraint_additions_are_guarded(sql_text):
    """Migration 043 must skip ADDing if any UNIQUE constraint with the same
    column composition already exists, regardless of constraint name.
    """
    # Each ADD section must (a) reference its target table and (b) introspect
    # pg_constraint by column composition (the ARRAY[...] check) inside an
    # `IF NOT EXISTS` block before the ALTER TABLE ... ADD CONSTRAINT.
    for table, col_a, col_b in (
        ("paper_user_state", "paper_id", "user_id"),
        ("paper_summaries", "paper_id", "user_id"),
        ("pulse_decks", "deck_date", "user_id"),
    ):
        pattern = (
            rf"rel\.relname\s*=\s*'{table}'.*?"
            rf"ARRAY\['{col_a}',\s*'{col_b}'\]"
            rf".*?ALTER TABLE {table}\s+ADD CONSTRAINT"
        )
        assert re.search(
            pattern,
            sql_text,
            re.IGNORECASE | re.DOTALL,
        ), f"Missing column-composition pg_constraint guard before ADD on {table}"


# ---------------------------------------------------------------------------
# Semantic contract (live-DB-only note)
# ---------------------------------------------------------------------------


def test_migration_043_allows_two_users_same_paper_user_state():
    """SEMANTIC CONTRACT (host mock only): after migration 043, inserting the same
    paper_id with two different user_id values must NOT violate the unique constraint.

    This is validated at the SQL-structure level above:
    - The old UNIQUE(paper_id) constraint is dropped via defensive PL/pgSQL that
      introspects pg_constraint — name-agnostic (H5 fix).
    - A new UNIQUE NULLS NOT DISTINCT (paper_id, user_id) is added.

    With two rows (paper_id=1, user_id=1) and (paper_id=1, user_id=2), the
    composite key differs so no conflict is raised.  The live integration test
    (pytest-postgresql fixture) is deferred to a future sprint and runs inside
    Docker where a real PostgreSQL 16 instance is available.
    """
    # Structural assertion: defensive PL/pgSQL block targets paper_user_state
    # and composite constraint replaces the per-paper one.
    sql = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert re.search(r"rel\.relname\s*=\s*'paper_user_state'", sql), (
        "PL/pgSQL block for paper_user_state not found"
    )
    assert "UNIQUE NULLS NOT DISTINCT (paper_id, user_id)" in sql or re.search(
        r"UNIQUE NULLS NOT DISTINCT\s*\(paper_id,\s*user_id\)", sql
    ), "Composite UNIQUE NULLS NOT DISTINCT not found"
