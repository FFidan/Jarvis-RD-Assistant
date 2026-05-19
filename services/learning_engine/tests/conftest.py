"""Shared test fixtures for learning_engine tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies are installed on the host venv — no stubs or
path manipulation needed here.

Infrastructure helpers (FakeRecord, _make_pool_and_conn, live_pg_dsn) are
re-exported from jarvis_common.testing so that ``from tests.conftest import
<symbol>`` resolves identically regardless of which conftest pytest loads
first (--import-mode=importlib + shared tests namespace invariant).
"""

import pytest

# Re-export canonical shared fixtures — keep these names stable; 76 test
# files import them directly via ``from tests.conftest import …``.
from jarvis_common.testing import (  # noqa: F401
    FakeRecord,
    _make_pool_and_conn,
    make_pool_and_conn,
)

# live_pg_dsn fixture for this service uses the "jarvis-le" container prefix.
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn

live_pg_dsn = _make_live_pg_dsn("jarvis-le")


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord
