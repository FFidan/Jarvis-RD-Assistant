"""Shared test fixtures for telegram_bot tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies (telegram, apscheduler) are installed on the host
venv — no module stubs are needed.

Infrastructure helpers (live_pg_dsn) are re-exported from
jarvis_common.testing so that the fixture is consistent across services
(--import-mode=importlib + shared tests namespace invariant).
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

# live_pg_dsn fixture for this service uses the "jarvis-rd" container prefix
# (matches the paper_ingestion service; telegram_bot has no independent PG fixtures).
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn

live_pg_dsn = _make_live_pg_dsn("jarvis-rd")

# Contract-layer fixtures: session-scoped Postgres + per-test txn rollback
from jarvis_common.testing import (  # noqa: E402, F401
    _make_contract_conn_fixture,
    _make_contract_pool_fixture,
    _make_contract_two_users_fixture,
)
from jarvis_common.testing import make_contract_pg_dsn as _make_contract_pg_dsn  # noqa: E402
from telegram_bot.handlers import rate_limit as _rate_limit_mod  # noqa: E402

contract_pg_dsn = _make_contract_pg_dsn("jarvis-tg-contract")
_contract_pool = _make_contract_pool_fixture()
contract_conn = _make_contract_conn_fixture()
contract_two_users = _make_contract_two_users_fixture()


@pytest.fixture()
def _clear_rate_limit_state() -> Iterator[None]:
    """Reset Telegram handler rate-limit memory around one test."""
    _rate_limit_mod._timestamps.clear()
    yield
    _rate_limit_mod._timestamps.clear()


@pytest.fixture(autouse=True)
def _patch_platform_event():
    """Suppress Platform event delivery in unit tests.

    Unit tests use lightweight clients that do not expose a Platform transport.

    Tests that specifically need to assert on event calls should override
    this patch within their own ``with patch(...)`` context.
    """
    with patch(
        "telegram_bot.handlers.commands._auth.record_event",
        new_callable=AsyncMock,
    ):
        yield
