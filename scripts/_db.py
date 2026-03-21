"""Shared database helpers for standalone scripts."""

from __future__ import annotations

import os


def get_dsn() -> str:
    """Build a PostgreSQL DSN from environment variables."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "jarvis")
    password = os.environ.get("PGPASSWORD", "")
    database = os.environ.get("PGDATABASE", "jarvis")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"
