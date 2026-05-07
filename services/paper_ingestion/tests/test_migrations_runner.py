"""Tests for migrations_runner.py — guards against migration version collisions."""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg
import pytest
from paper_ingestion.migrations_runner import (
    _MIGRATION_SCHEMA_PROBES,
    _repair_false_applied_migrations,
)


def test_no_duplicate_migration_versions() -> None:
    """Each migration file in db/migrations/ must have a unique numeric prefix.

    Catches future collisions at CI time so the migrations_runner's runtime
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


def test_init_sql_uses_explicit_embodied_bootstrap_versions() -> None:
    """init.sql must not blanket-mark migrations that its schema does not contain."""
    repo_root = Path(__file__).resolve().parents[3]
    init_sql = (repo_root / "db" / "init.sql").read_text(encoding="utf-8")
    bootstrap_sql = init_sql.split("SCHEMA-MIGRATIONS BOOTSTRAP", maxsplit=1)[1]
    executable_bootstrap_sql = "\n".join(
        line for line in bootstrap_sql.splitlines() if not line.lstrip().startswith("--")
    )

    assert "generate_series" not in executable_bootstrap_sql

    seeded_versions = {int(value) for value in re.findall(r"\((\d+)\)", executable_bootstrap_sql)}
    assert set(range(1, 33)).issubset(seeded_versions)
    assert set(range(34, 49)).issubset(seeded_versions)
    # 33 is intentionally absent (false-applied, repaired at runtime).
    assert 33 not in seeded_versions
    # 49-51 and 54-60 are now baked into init.sql.
    assert {49, 50, 51, 54, 55, 56, 57, 58, 59, 60}.issubset(seeded_versions)
    # 52 (procrastinate schema) and 53 (drop legacy jobs) are NOT baked into
    # init.sql; the runtime runner applies them on first boot.
    assert seeded_versions.isdisjoint({52, 53})


def test_init_sql_seed_list_covers_up_to_latest_migration() -> None:
    """The schema_migrations seed list must cover all migration versions that
    are baked into init.sql.  As a lightweight regression guard, assert that
    the highest version in the seed list is >= the highest version present in
    db/migrations/ (minus the versions intentionally deferred to the runner).

    Specifically: versions 52 and 53 are deferred; everything else up to the
    latest migration file should appear in the seed.
    """
    repo_root = Path(__file__).resolve().parents[3]
    migrations_dir = repo_root / "db" / "migrations"
    init_sql = (repo_root / "db" / "init.sql").read_text(encoding="utf-8")

    bootstrap_sql = init_sql.split("SCHEMA-MIGRATIONS BOOTSTRAP", maxsplit=1)[1]
    executable_bootstrap_sql = "\n".join(
        line for line in bootstrap_sql.splitlines() if not line.lstrip().startswith("--")
    )
    seeded_versions = {int(v) for v in re.findall(r"\((\d+)\)", executable_bootstrap_sql)}

    # Collect all migration versions from db/migrations/*.sql
    file_versions: list[int] = []
    for sql_file in migrations_dir.glob("*.sql"):
        try:
            file_versions.append(int(sql_file.name.split("_")[0]))
        except (ValueError, IndexError):
            continue

    if not file_versions:
        return  # nothing to check

    max_migration = max(file_versions)
    # Versions intentionally deferred to the runtime runner (not baked into init.sql).
    deferred = {33, 52, 53}
    required = {v for v in range(1, max_migration + 1) if v not in deferred}
    missing = required - seeded_versions
    assert not missing, (
        f"init.sql seed list is missing versions that should be baked in: {sorted(missing)}. "
        f"Either bake their schema into init.sql and add them to the seed list, or add them "
        f"to `deferred` in this test if they are intentionally applied by the runtime runner."
    )


def test_false_applied_repair_does_not_probe_mutable_config_values() -> None:
    """Migration repair probes must only use durable schema evidence."""
    probe_versions = {version for version, _, _ in _MIGRATION_SCHEMA_PROBES}

    assert 56 not in probe_versions


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
async def test_repair_false_applied_migrations_removes_failed_probe_rows() -> None:
    """A false applied row is removed so run_migrations can replay the SQL file."""
    probes = {version: probe_sql for version, _, probe_sql in _MIGRATION_SCHEMA_PROBES}
    conn = _FakeConnection(
        applied_versions={33, 52},
        probe_results={
            probes[33]: False,
            probes[52]: True,
        },
    )

    await _repair_false_applied_migrations(conn)  # type: ignore[arg-type]

    assert conn.deleted_versions == [33]
    assert any("DELETE FROM schema_migrations" in s for s in conn.executed_sqls)


@pytest.mark.asyncio
async def test_repair_false_applied_migrations_treats_missing_dependencies_as_failed_probe() -> (
    None
):
    """A missing dependent table/object should not abort startup repair."""
    probes = {version: probe_sql for version, _, probe_sql in _MIGRATION_SCHEMA_PROBES}
    conn = _FakeConnection(
        applied_versions={57},
        probe_results={probes[57]: asyncpg.UndefinedTableError("user_config missing")},
    )

    await _repair_false_applied_migrations(conn)  # type: ignore[arg-type]

    assert conn.deleted_versions == [57]


@pytest.mark.asyncio
async def test_migration_049_probe_requires_recommendation_feedback_and_no_pulse_ratings() -> None:
    """049 is false-applied if the target exists but legacy pulse_ratings still exists."""
    probes = {version: probe_sql for version, _, probe_sql in _MIGRATION_SCHEMA_PROBES}
    probe_049 = probes[49]

    assert "recommendation_feedback" in probe_049
    assert "pulse_ratings" in probe_049

    conn = _FakeConnection(
        applied_versions={49},
        probe_results={probe_049: False},
    )

    await _repair_false_applied_migrations(conn)  # type: ignore[arg-type]

    assert conn.deleted_versions == [49]


@pytest.mark.asyncio
async def test_migration_058_probe_requires_job_terminal_columns() -> None:
    """058 is false-applied if job_progress lacks terminal result/error columns."""
    probes = {version: probe_sql for version, _, probe_sql in _MIGRATION_SCHEMA_PROBES}
    probe_058 = probes[58]

    assert "job_progress" in probe_058
    assert "result" in probe_058
    assert "error" in probe_058

    conn = _FakeConnection(
        applied_versions={58},
        probe_results={probe_058: False},
    )

    await _repair_false_applied_migrations(conn)  # type: ignore[arg-type]

    assert conn.deleted_versions == [58]
