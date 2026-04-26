"""Tests for migrations_runner.py — guards against migration version collisions."""

from __future__ import annotations

from pathlib import Path


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
