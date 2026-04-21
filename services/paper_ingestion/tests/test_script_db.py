"""Direct tests for the standalone script database helper."""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ lives at the repo root, which is not in pytest's pythonpath.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts._db import get_dsn


def test_get_dsn_prefers_database_url(monkeypatch):
    """DATABASE_URL should take precedence over individual PG* variables."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://db-url")
    monkeypatch.setenv("PGHOST", "ignored-host")

    assert get_dsn() == "postgresql://db-url"


def test_get_dsn_builds_from_pg_components(monkeypatch):
    """get_dsn should compose the fallback DSN from the PG* environment."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGPORT", "5433")
    monkeypatch.setenv("PGUSER", "jarvis")
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setenv("PGDATABASE", "research")

    assert get_dsn() == "postgresql://jarvis:secret@db:5433/research"


def test_get_dsn_uses_defaults_for_missing_pg_components(monkeypatch):
    """Missing PG* values should fall back to the script defaults."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)
    monkeypatch.delenv("PGUSER", raising=False)
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)

    assert get_dsn() == "postgresql://jarvis:@localhost:5432/jarvis"
