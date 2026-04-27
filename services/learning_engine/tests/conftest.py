"""Shared test fixtures for learning_engine tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies are installed on the host venv — no stubs or
path manipulation needed here.
"""

from unittest.mock import AsyncMock, MagicMock

import jarvis_common.jobs as _jobs_module
import pytest

# ---------------------------------------------------------------------------
# _HANDLERS isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _reset_job_handlers():
    """Snapshot and restore jarvis_common.jobs._HANDLERS around each test.

    Use this fixture in any test that registers new job handlers, to prevent
    cross-test _HANDLERS state pollution.  Never rely on global _HANDLERS state
    across tests — always opt-in to this fixture when your test touches handler
    registration.
    """
    snapshot = dict(_jobs_module._HANDLERS)
    yield
    _jobs_module._HANDLERS.clear()
    _jobs_module._HANDLERS.update(snapshot)


# ---------------------------------------------------------------------------
# FakeRecord + shared fixtures
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
