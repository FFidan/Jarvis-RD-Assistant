"""Shared test fixtures for learning_engine tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies are installed on the host venv — no stubs or
path manipulation needed here.

Infrastructure helpers (FakeRecord, _make_pool_and_conn, live_pg_dsn) are
re-exported from jarvis_common.testing so that ``from tests.conftest import
<symbol>`` resolves identically regardless of which conftest pytest loads
first (--import-mode=importlib + shared tests namespace invariant).

NOTE: LE-specific row factories (make_card_row, make_job_ctx) live in
le_helpers.py — NOT here — because tests.conftest symbols must exist in both
the paper_ingestion and learning_engine conftest files (shared ``tests``
namespace under --import-mode=importlib).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

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

# Contract-layer fixtures (Wave 4): session-scoped Postgres + per-test txn rollback
from jarvis_common.testing import (  # noqa: E402, F401
    _make_contract_conn_fixture,
    _make_contract_pool_fixture,
    _make_contract_two_users_fixture,
)
from jarvis_common.testing import make_contract_pg_dsn as _make_contract_pg_dsn  # noqa: E402
from jarvis_common.testing_contract_apps import (  # noqa: E402
    configure_contract_api_key,
    make_contract_client,
    patch_app_state,
    patch_dependency_overrides,
)

contract_pg_dsn = _make_contract_pg_dsn("jarvis-le-contract")
_contract_pool = _make_contract_pool_fixture()
contract_conn = _make_contract_conn_fixture()
contract_two_users = _make_contract_two_users_fixture()


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    """Configure the standard contract-test API key for ASGI clients."""
    with configure_contract_api_key(monkeypatch) as key:
        yield key


def _client(app, cookie: str):
    """Return the standard contract-test ASGI client for LE contract tests."""
    return make_contract_client(app, cookie)


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app(contract_conn):
    """Wire the LE app to the per-test contract connection."""
    from jarvis_common.testing import SharedConnPool
    from learning_engine.deps import get_anki_exporter, get_db_pool, get_fsrs_manager
    from learning_engine.deps import limiter
    from learning_engine.main import app

    shared = SharedConnPool(contract_conn)
    now = datetime.now(UTC)
    mock_fsrs = MagicMock()
    mock_fsrs.create_new_card.return_value = ({}, now)
    mock_fsrs.schedule_review.return_value = ({}, {}, now + timedelta(days=1))
    mock_exporter = MagicMock()
    mock_exporter.export_deck.return_value = bytes.fromhex("504b0506") + b"\x00" * 18

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False
    try:
        with (
            patch_app_state(
                app,
                {
                    "db_pool": shared,
                    "http_client": AsyncMock(),
                    "fsrs_manager": mock_fsrs,
                    "anki_exporter": mock_exporter,
                    "card_generator": AsyncMock(),
                },
            ),
            patch_dependency_overrides(
                app,
                set_overrides={
                    get_db_pool: lambda: shared,
                    get_fsrs_manager: lambda: mock_fsrs,
                    get_anki_exporter: lambda: mock_exporter,
                },
            ),
        ):
            yield app
    finally:
        limiter.enabled = limiter_was_enabled


# Composable contract-app fixture wiring the LE app to a FauxLiteLLMServer
# sidecar. Yields (app, faux_server) for W2+ tests that need to script LLM
# responses without touching real LiteLLM infrastructure.
from jarvis_common.testing import (  # noqa: E402, F401
    _make_le_contract_app_with_litellm_sidecar,
)
from learning_engine._state import reset_services as _le_reset_services  # noqa: E402
from learning_engine._state import set_services as _le_set_services  # noqa: E402

le_contract_app_with_litellm_sidecar = _make_le_contract_app_with_litellm_sidecar(
    _le_set_services, _le_reset_services
)


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord
