"""Structural tests for migration 021 tracked_authors uniqueness."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"
_MIGRATION_FILE = _MIGRATIONS_DIR / "021_tracked_authors_nulls_not_distinct.sql"


@pytest.fixture(scope="module")
def sql_text() -> str:
    """Return the migration SQL under test."""
    assert _MIGRATION_FILE.exists(), f"Missing: {_MIGRATION_FILE}"
    return _MIGRATION_FILE.read_text(encoding="utf-8")


def test_deduplicates_before_adding_constraint(sql_text: str) -> None:
    """Migration must keep one row per logical author before adding uniqueness."""
    assert re.search(
        r"ROW_NUMBER\(\)\s+OVER\s*\(\s*PARTITION BY author_name,\s*s2_author_id",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"DELETE FROM tracked_authors\s+\w+\s+USING ranked",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    )


def test_adds_unique_nulls_not_distinct(sql_text: str) -> None:
    """Migration must enforce Postgres NULLS NOT DISTINCT semantics."""
    assert re.search(
        r"UNIQUE NULLS NOT DISTINCT\s*\(author_name,\s*s2_author_id\)",
        sql_text,
        re.IGNORECASE,
    )


def test_fresh_init_constraint_guard(sql_text: str) -> None:
    """Fresh installs already have the named constraint from init.sql."""
    assert "pg_constraint" in sql_text
    assert "tracked_authors_name_s2_unique" in sql_text
    assert re.search(
        r"IF NOT EXISTS\s*\(.*tracked_authors_name_s2_unique.*\)\s+THEN\s+ALTER TABLE",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    )
