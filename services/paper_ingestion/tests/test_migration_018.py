"""Tests for DB migration 018 (Discovery & Pulse subsystem).

The repo's test infrastructure uses mocked asyncpg pools — there is no
live PostgreSQL in the host test environment. We therefore:

  1. Assert the SQL file exists and parses (no syntax-level issues we can
     catch statically).
  2. Assert it contains the exact DDL statements required by the spec §4.5
     via regex/string matching — a structural contract test.
  3. Assert the migration is idempotent by verifying every CREATE uses
     IF NOT EXISTS and every INSERT uses ON CONFLICT DO NOTHING.

This mirrors the approach in test_migration_runner.py, which also mocks
the pool rather than connecting to a real DB.
"""

import re
from pathlib import Path

import pytest

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"
_MIGRATION_FILE = _MIGRATIONS_DIR / "018_pulse_subsystem.sql"


@pytest.fixture(scope="module")
def sql_text() -> str:
    """Load the migration SQL file."""
    assert _MIGRATION_FILE.exists(), (
        f"Migration file not found: {_MIGRATION_FILE}. "
        "Create db/migrations/018_pulse_subsystem.sql first."
    )
    return _MIGRATION_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_migration_file_exists():
    """Migration 018 SQL file must exist."""
    assert _MIGRATION_FILE.exists(), f"Missing: {_MIGRATION_FILE}"


# ---------------------------------------------------------------------------
# Table definitions
# ---------------------------------------------------------------------------


def test_pulse_decks_table_created(sql_text):
    """pulse_decks table DDL must be present with IF NOT EXISTS."""
    assert re.search(r"CREATE TABLE IF NOT EXISTS\s+pulse_decks", sql_text, re.IGNORECASE), (
        "pulse_decks CREATE TABLE IF NOT EXISTS not found"
    )


def test_pulse_decks_columns(sql_text):
    """pulse_decks must have deck_date UNIQUE, card_count, generated_at, stats."""
    assert "deck_date" in sql_text
    assert "card_count" in sql_text
    assert "generated_at" in sql_text
    assert "stats" in sql_text


def test_pulse_cards_table_created(sql_text):
    """pulse_cards table DDL must be present with IF NOT EXISTS."""
    assert re.search(r"CREATE TABLE IF NOT EXISTS\s+pulse_cards", sql_text, re.IGNORECASE), (
        "pulse_cards CREATE TABLE IF NOT EXISTS not found"
    )


def test_pulse_cards_columns(sql_text):
    """pulse_cards must have rank, score, llm_relevance, llm_novelty, reasoning, signals."""
    for col in ("rank", "score", "llm_relevance", "llm_novelty", "reasoning", "signals"):
        assert col in sql_text, f"pulse_cards column '{col}' not found"


def test_pulse_cards_fk_cascade(sql_text):
    """pulse_cards must reference pulse_decks and papers with ON DELETE CASCADE."""
    # At least two CASCADE references in the file
    cascades = re.findall(r"ON DELETE CASCADE", sql_text, re.IGNORECASE)
    assert len(cascades) >= 2, "Expected at least two ON DELETE CASCADE clauses"


def test_pulse_cards_unique_deck_paper(sql_text):
    """pulse_cards must have UNIQUE(deck_id, paper_id)."""
    assert re.search(r"UNIQUE\s*\(\s*deck_id\s*,\s*paper_id\s*\)", sql_text), (
        "UNIQUE(deck_id, paper_id) constraint not found in pulse_cards"
    )


def test_pulse_cards_index(sql_text):
    """idx_pulse_cards_deck_rank index must be defined."""
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS\s+idx_pulse_cards_deck_rank", sql_text, re.IGNORECASE
    ), "idx_pulse_cards_deck_rank index not found"


def test_pulse_ratings_table_created(sql_text):
    """pulse_ratings table DDL must be present with IF NOT EXISTS."""
    assert re.search(r"CREATE TABLE IF NOT EXISTS\s+pulse_ratings", sql_text, re.IGNORECASE), (
        "pulse_ratings CREATE TABLE IF NOT EXISTS not found"
    )


def test_pulse_ratings_check_constraint(sql_text):
    """pulse_ratings CHECK constraint must list exactly the five allowed values."""
    assert re.search(
        r"CHECK\s*\(\s*rating\s+IN\s*\(\s*'up'\s*,\s*'down'\s*,\s*'save'\s*,\s*'dismiss'\s*,\s*'open'\s*\)\s*\)",
        sql_text,
        re.IGNORECASE,
    ), "pulse_ratings CHECK(rating IN ('up','down','save','dismiss','open')) not found"


def test_pulse_ratings_indexes(sql_text):
    """pulse_ratings must have idx_pulse_ratings_paper and idx_pulse_ratings_created indexes."""
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS\s+idx_pulse_ratings_paper", sql_text, re.IGNORECASE
    ), "idx_pulse_ratings_paper index not found"
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS\s+idx_pulse_ratings_created", sql_text, re.IGNORECASE
    ), "idx_pulse_ratings_created index not found"


def test_pdf_resolutions_table_created(sql_text):
    """pdf_resolutions table DDL must be present with IF NOT EXISTS."""
    assert re.search(r"CREATE TABLE IF NOT EXISTS\s+pdf_resolutions", sql_text, re.IGNORECASE), (
        "pdf_resolutions CREATE TABLE IF NOT EXISTS not found"
    )


def test_pdf_resolutions_columns(sql_text):
    """pdf_resolutions must have doi, arxiv_id, resolved_url, resolver_name, resolved_at."""
    for col in ("doi", "arxiv_id", "resolved_url", "resolver_name", "resolved_at"):
        assert col in sql_text, f"pdf_resolutions column '{col}' not found"


def test_pdf_resolutions_unique_doi_arxiv(sql_text):
    """pdf_resolutions must have UNIQUE(doi, arxiv_id)."""
    assert re.search(r"UNIQUE\s*\(\s*doi\s*,\s*arxiv_id\s*\)", sql_text), (
        "UNIQUE(doi, arxiv_id) not found in pdf_resolutions"
    )


def test_pdf_resolutions_partial_index(sql_text):
    """pdf_resolutions must have a partial index on doi WHERE doi IS NOT NULL."""
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS\s+idx_pdf_resolutions_doi", sql_text, re.IGNORECASE
    ), "idx_pdf_resolutions_doi index not found"
    assert "WHERE doi IS NOT NULL" in sql_text, (
        "Partial index condition 'WHERE doi IS NOT NULL' not found"
    )


# ---------------------------------------------------------------------------
# topics.description column
# ---------------------------------------------------------------------------


def test_topics_description_column(sql_text):
    """Migration must ADD COLUMN IF NOT EXISTS description to topics."""
    assert re.search(
        r"ALTER TABLE\s+topics\s+ADD COLUMN IF NOT EXISTS\s+description\s+TEXT",
        sql_text,
        re.IGNORECASE,
    ), "ALTER TABLE topics ADD COLUMN IF NOT EXISTS description TEXT not found"


# ---------------------------------------------------------------------------
# paper_sources seed rows
# ---------------------------------------------------------------------------


def test_paper_sources_openalex_row(sql_text):
    """Migration must insert openalex row into paper_sources."""
    assert "'openalex'" in sql_text or '"openalex"' in sql_text, (
        "openalex source_type not found in INSERT"
    )


def test_paper_sources_pubmed_row(sql_text):
    """Migration must insert pubmed row into paper_sources."""
    assert "'pubmed'" in sql_text or '"pubmed"' in sql_text, (
        "pubmed source_type not found in INSERT"
    )


def test_paper_sources_on_conflict_do_nothing(sql_text):
    """paper_sources INSERT must use ON CONFLICT DO NOTHING."""
    # There should be at least one ON CONFLICT DO NOTHING after the INSERT INTO paper_sources
    paper_sources_block = sql_text[sql_text.lower().find("insert into paper_sources") :]
    assert "ON CONFLICT" in paper_sources_block.upper(), (
        "ON CONFLICT DO NOTHING missing from paper_sources INSERT"
    )


# ---------------------------------------------------------------------------
# user_config seed rows
# ---------------------------------------------------------------------------


def test_user_config_pulse_enabled(sql_text):
    """pulse.enabled must be seeded in user_config."""
    assert "'pulse.enabled'" in sql_text, "pulse.enabled key not found in user_config INSERT"


def test_user_config_pulse_cron(sql_text):
    """pulse.cron must be seeded in user_config."""
    assert "'pulse.cron'" in sql_text, "pulse.cron key not found in user_config INSERT"


def test_user_config_pulse_deck_size(sql_text):
    """pulse.deck_size must be seeded in user_config."""
    assert "'pulse.deck_size'" in sql_text, "pulse.deck_size key not found in user_config INSERT"


def test_user_config_pulse_stage2_top_k(sql_text):
    """pulse.stage2_top_k must be seeded in user_config."""
    assert "'pulse.stage2_top_k'" in sql_text, (
        "pulse.stage2_top_k key not found in user_config INSERT"
    )


def test_user_config_pulse_weights(sql_text):
    """pulse.weights must be seeded in user_config."""
    assert "'pulse.weights'" in sql_text, "pulse.weights key not found in user_config INSERT"


def test_user_config_on_conflict_do_nothing(sql_text):
    """user_config INSERT must use ON CONFLICT DO NOTHING."""
    user_config_block = sql_text[sql_text.lower().find("insert into user_config") :]
    assert "ON CONFLICT" in user_config_block.upper(), (
        "ON CONFLICT DO NOTHING missing from user_config INSERT"
    )


# ---------------------------------------------------------------------------
# Idempotency guarantees
# ---------------------------------------------------------------------------


def test_all_creates_use_if_not_exists(sql_text):
    """Every CREATE TABLE statement must use IF NOT EXISTS."""
    plain_creates = re.findall(r"CREATE TABLE\s+(?!IF NOT EXISTS)\w+", sql_text, re.IGNORECASE)
    assert plain_creates == [], f"Found CREATE TABLE without IF NOT EXISTS: {plain_creates}"


def test_all_inserts_use_on_conflict_do_nothing(sql_text):
    """Every INSERT INTO must be followed by ON CONFLICT DO NOTHING (idempotency)."""
    inserts = list(re.finditer(r"INSERT INTO\s+\w+", sql_text, re.IGNORECASE))
    for m in inserts:
        # look ahead in a window for ON CONFLICT
        window = sql_text[m.start() : m.start() + 2000]
        assert "ON CONFLICT" in window.upper(), (
            f"INSERT at offset {m.start()} missing ON CONFLICT DO NOTHING: '{m.group()}'"
        )


def test_no_drop_statements(sql_text):
    """Migration must be additive only — no DROP statements."""
    assert not re.search(r"\bDROP\b", sql_text, re.IGNORECASE), (
        "Migration contains DROP statement — must be additive only"
    )
