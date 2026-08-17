"""Tests for the shared migration runner and migration version collisions."""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg
import pytest
from jarvis_common.migrations import (
    _MIGRATION_SCHEMA_PROBES,
    _repair_false_applied_migrations,
)


def test_no_duplicate_migration_versions() -> None:
    """Each migration file in db/migrations/ must have a unique numeric prefix.

    Catches future collisions at CI time so the migration runner's runtime
    guard is never triggered in production.
    """
    migrations_dir = Path(__file__).resolve().parents[3] / "db" / "migrations"
    assert migrations_dir.exists(), f"Migrations directory not found: {migrations_dir}"

    seen: dict[int, str] = {}
    duplicates: list[str] = []

    for sql_file in sorted(migrations_dir.glob("*.sql")):
        try:
            version = int(sql_file.name.split("_")[0])
        except (ValueError, IndexError):
            continue  # non-migration file — not relevant to version collision check

        if version in seen:
            duplicates.append(f"version {version}: {sql_file.name} conflicts with {seen[version]}")
        else:
            seen[version] = sql_file.name

    assert not duplicates, "Duplicate migration versions found:\n" + "\n".join(duplicates)


def test_paper_chunks_have_no_user_ownership_in_fresh_or_upgraded_schema() -> None:
    """Canonical chunks must survive deletion of the user who first created them."""
    repo_root = Path(__file__).resolve().parents[3]
    init_sql = (repo_root / "db" / "init.sql").read_text()
    chunk_table = init_sql.split("CREATE TABLE public.paper_chunks (", 1)[1].split(");", 1)[0]

    assert "user_id" not in chunk_table
    assert "CREATE INDEX idx_paper_chunks_user" not in init_sql
    assert "ADD CONSTRAINT paper_chunks_user_id_fkey" not in init_sql

    migration = (
        repo_root / "db" / "migrations" / "0104_drop_paper_chunks_user_ownership.sql"
    ).read_text()
    assert migration.count("ALTER TABLE IF EXISTS paper_chunks") == 2
    assert "DROP COLUMN IF EXISTS user_id" in migration


_BOOTSTRAP_SEED_LO = 1
_BOOTSTRAP_SEED_HI = 119  # fresh init premarks the complete 1..118 baseline


def test_init_sql_uses_explicit_embodied_bootstrap_versions() -> None:
    """init.sql bootstrap must use an explicit version list, not generate_series.

    Fresh init embodies and premarks the complete schema through version 118.
    The seeded set must be exactly set(range(_BOOTSTRAP_SEED_LO,
    _BOOTSTRAP_SEED_HI)) — contiguous, no gaps.
    """
    repo_root = Path(__file__).resolve().parents[3]
    init_sql = (repo_root / "db" / "init.sql").read_text(encoding="utf-8")
    bootstrap_sql = init_sql.split("SCHEMA-MIGRATIONS BOOTSTRAP", maxsplit=1)[1]
    executable_bootstrap_sql = "\n".join(
        line for line in bootstrap_sql.splitlines() if not line.lstrip().startswith("--")
    )

    assert "generate_series" not in executable_bootstrap_sql

    seeded_versions = {int(value) for value in re.findall(r"\((\d+)\)", executable_bootstrap_sql)}
    expected = set(range(_BOOTSTRAP_SEED_LO, _BOOTSTRAP_SEED_HI))
    assert seeded_versions == expected, (
        f"init.sql bootstrap must seed exactly versions "
        f"{_BOOTSTRAP_SEED_LO}..{_BOOTSTRAP_SEED_HI - 1}. "
        f"Missing: {sorted(expected - seeded_versions)}. "
        f"Extra: {sorted(seeded_versions - expected)}."
    )


def test_init_sql_seed_list_covers_up_to_latest_migration() -> None:
    """The schema_migrations seed in init.sql must own the full consolidated baseline.

    Fresh init embodies and premarks the complete schema through version 118.
    - init.sql premarks _BOOTSTRAP_SEED_LO.._BOOTSTRAP_SEED_HI-1.
    - Retained migration files may also be premarked: they are needed for
      in-place upgrades, while fresh installs already embody their effects.
    - No gaps exist in the seeded range.
    """
    repo_root = Path(__file__).resolve().parents[3]
    migrations_dir = repo_root / "db" / "migrations"
    init_sql = (repo_root / "db" / "init.sql").read_text(encoding="utf-8")

    bootstrap_sql = init_sql.split("SCHEMA-MIGRATIONS BOOTSTRAP", maxsplit=1)[1]
    executable_bootstrap_sql = "\n".join(
        line for line in bootstrap_sql.splitlines() if not line.lstrip().startswith("--")
    )
    seeded_versions = {int(v) for v in re.findall(r"\((\d+)\)", executable_bootstrap_sql)}

    expected = set(range(_BOOTSTRAP_SEED_LO, _BOOTSTRAP_SEED_HI))
    assert seeded_versions == expected, (
        f"init.sql seed list must be exactly "
        f"{{{_BOOTSTRAP_SEED_LO}..{_BOOTSTRAP_SEED_HI - 1}}}. "
        f"Missing: {sorted(expected - seeded_versions)}. "
        f"Extra: {sorted(seeded_versions - expected)}."
    )

    file_versions: list[int] = []
    for sql_file in migrations_dir.glob("*.sql"):
        try:
            file_versions.append(int(sql_file.name.split("_")[0]))
        except (ValueError, IndexError):
            continue

    assert set(file_versions) <= seeded_versions


# test_schema_probes_cover_recent_migrations — DELETED (db/migrations squash 2026-05-19):
# chain-coupled: asserted {56,60,61} <= versions on _MIGRATION_SCHEMA_PROBES which is
# now an empty tuple. Probe data retired with the 88-file chain.

# test_migration_060_probe_checks_integer_with_schema_filter — DELETED (db/migrations squash 2026-05-19):
# chain-coupled: probes[60] on an empty tuple is a KeyError. Probe data retired.


def test_strip_outer_transaction_control_same_line_dollar_quote() -> None:
    """BEGIN/COMMIT outside dollar-quoted blocks are stripped even when a $$-pair
    opens and closes on the same line as other SQL.

    Regression for the old per-line count approach: it correctly toggled twice and
    ended up with in_dollar=False, but the check was done *after* toggling, meaning
    a lone BEGIN on a subsequent line would still be stripped correctly. The new
    state machine must handle same-line open+close without leaking text.
    """
    from jarvis_common.migrations import _strip_outer_transaction_control

    sql = "BEGIN;\nDO $$ BEGIN RAISE NOTICE 'hello'; END $$;\nSELECT 1;\nCOMMIT;\n"
    result = _strip_outer_transaction_control(sql)
    # Transaction control lines must be gone
    assert "BEGIN;" not in result
    assert "COMMIT;" not in result
    # The inline DO block content must be preserved
    assert "RAISE NOTICE" in result
    assert "SELECT 1;" in result


class _FakeConnection:
    def __init__(
        self,
        applied_versions: set[int],
        probe_results: dict[str, bool | Exception],
    ) -> None:
        self.applied_versions = applied_versions
        self.probe_results = probe_results
        self.deleted_versions: list[int] = []
        self.executed_sqls: list[str] = []

    async def fetch(self, _sql: str, versions: list[int]) -> list[dict[str, int]]:
        return [{"version": version} for version in versions if version in self.applied_versions]

    async def fetchval(self, sql: str) -> bool:
        result = self.probe_results[sql]
        if isinstance(result, Exception):
            raise result
        return result

    async def execute(self, sql: str, version: int) -> None:
        self.executed_sqls.append(sql)
        self.deleted_versions.append(version)


@pytest.mark.asyncio
async def test_repair_false_applied_migrations_no_ops_on_empty_probes() -> None:
    """With an empty probe list (post-squash), _repair_false_applied_migrations
    is a safe no-op — nothing to probe, nothing to delete."""
    # _MIGRATION_SCHEMA_PROBES is now () — the chain is squashed.
    assert _MIGRATION_SCHEMA_PROBES == (), (
        "_MIGRATION_SCHEMA_PROBES must be empty post-squash; runner kept for 89+"
    )
    conn = _FakeConnection(
        applied_versions={1, 2, 88},
        probe_results={},
    )

    await _repair_false_applied_migrations(conn)  # type: ignore[arg-type]

    # Empty probes → no DELETE calls.
    assert conn.deleted_versions == []


@pytest.mark.asyncio
async def test_repair_false_applied_migrations_treats_missing_dependencies_as_failed_probe() -> (
    None
):
    """_repair_false_applied_migrations must not crash when _MIGRATION_SCHEMA_PROBES is empty.

    Post-squash, the probe list is empty and the function is a no-op regardless of
    what versions are in schema_migrations. This guards against future accidental
    re-introduction of probe data that could trigger spurious DELETE calls.
    """
    conn = _FakeConnection(
        applied_versions={57},
        probe_results={},  # no probes registered post-squash
    )

    await _repair_false_applied_migrations(conn)  # type: ignore[arg-type]

    assert conn.deleted_versions == []


# test_migration_049_probe_requires_recommendation_feedback_and_no_pulse_ratings — DELETED
# (db/migrations squash 2026-05-19): chain-coupled probe test; probes[49] KeyErrors on empty tuple.

# test_migration_058_probe_requires_job_terminal_columns — DELETED
# (db/migrations squash 2026-05-19): chain-coupled probe test; probes[58] KeyErrors on empty tuple.


async def test_zotero_links_additive_structure_on_live_db(test_db_pool: asyncpg.Pool) -> None:
    """ADDITIVE structural assertion against a live DB: the link table + per-user
    indexes + kept global column come from the consolidated db/init.sql baseline
    (not from run_migrations()). Confirms paper_user_zotero_links, uq_pu_zotero_item,
    the re-scoped highlight index, and papers.zotero_item_key are all present.

    Gated by the test_db_pool fixture, which pytest.skips without JARVIS_RUN_LIVE_PG=1.
    """
    async with test_db_pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT to_regclass('research.paper_user_zotero_links')")
            is not None
        ), "paper_user_zotero_links table must exist"

        indexes = {
            r["indexname"]
            for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'research' "
                "AND tablename IN ('paper_user_zotero_links', 'paper_highlights')"
            )
        }
        assert "uq_pu_zotero_item" in indexes, "per-user Zotero item unique index must exist"
        assert "uq_paper_highlights_zotero_key" in indexes, (
            "re-scoped per-user highlight annotation index must exist"
        )
        assert "idx_paper_highlights_zotero_key" not in indexes, (
            "the old GLOBAL highlight annotation index must be dropped"
        )

        # ADDITIVE: the global source column is KEPT (vestigial), never dropped.
        assert (
            await conn.fetchval(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'research' "
                "AND table_name = 'papers' AND column_name = 'zotero_item_key'"
            )
            == 1
        ), "papers.zotero_item_key must still exist (additive-only migration)"
