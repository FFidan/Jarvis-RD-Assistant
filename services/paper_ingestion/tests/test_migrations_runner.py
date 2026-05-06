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
    assert seeded_versions.isdisjoint({33, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58})


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

    async def fetch(self, _sql: str, versions: list[int]) -> list[dict[str, int]]:
        return [{"version": version} for version in versions if version in self.applied_versions]

    async def fetchval(self, sql: str) -> bool:
        result = self.probe_results[sql]
        if isinstance(result, Exception):
            raise result
        return result

    async def execute(self, _sql: str, version: int) -> None:
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
