"""Structural tests for migration 045 search-preview functional indexes."""

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[3] / "db/migrations/045_papers_search_preview_indexes.sql"
)


def test_migration_045_creates_external_id_normalized_index() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE INDEX IF NOT EXISTS idx_papers_external_id_normalized" in sql
    assert "lower(btrim(external_id))" in sql


def test_migration_045_creates_metadata_doi_index() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE INDEX IF NOT EXISTS idx_papers_metadata_doi" in sql
    assert "lower(btrim(metadata->>'doi'))" in sql
    assert "WHERE metadata ? 'doi'" in sql


def test_migration_045_creates_metadata_arxiv_id_index() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE INDEX IF NOT EXISTS idx_papers_metadata_arxiv_id" in sql
    assert "lower(btrim(metadata->>'arxiv_id'))" in sql


def test_migration_045_creates_title_year_index() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE INDEX IF NOT EXISTS idx_papers_title_year_normalized" in sql
    assert "regexp_replace(lower(btrim(title))" in sql
    assert "EXTRACT(YEAR FROM published_date)" in sql


def test_init_sql_mirrors_migration_045() -> None:
    init_sql = (Path(__file__).resolve().parents[3] / "db/init.sql").read_text(encoding="utf-8")
    for index_name in (
        "idx_papers_external_id_normalized",
        "idx_papers_metadata_doi",
        "idx_papers_metadata_arxiv_id",
        "idx_papers_title_year_normalized",
    ):
        assert index_name in init_sql, f"db/init.sql must mirror migration 045 index {index_name}"
