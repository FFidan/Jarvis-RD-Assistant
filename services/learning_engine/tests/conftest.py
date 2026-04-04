"""Shared test fixtures for learning_engine tests.

Loaded automatically by pytest before any test file in this directory.
Learning engine does not import fitz/tiktoken/qdrant, so no module stubs
are needed here — only path setup and shared DB fixtures.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Path setup
# ---------------------------------------------------------------------------
_SERVICE_ROOT = str(Path(__file__).resolve().parents[1])
_JARVIS_COMMON = str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common")
for p in (_SERVICE_ROOT, _JARVIS_COMMON):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# 2. FakeRecord + shared fixtures
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Unified asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _make_pool_and_conn():
    """Create mock asyncpg Pool + Connection with transaction support."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord
