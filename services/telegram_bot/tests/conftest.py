"""Shared test fixtures for telegram_bot tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies (telegram, apscheduler) are installed on the host
venv — no module stubs are needed.

Infrastructure helpers (live_pg_dsn) are re-exported from
jarvis_common.testing so that the fixture is consistent across services
(--import-mode=importlib + shared tests namespace invariant).
"""

from unittest.mock import AsyncMock, patch

import pytest

# live_pg_dsn fixture for this service uses the "jarvis-rd" container prefix
# (matches the paper_ingestion service; telegram_bot has no independent PG fixtures).
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn

live_pg_dsn = _make_live_pg_dsn("jarvis-rd")


@pytest.fixture(autouse=True)
def _patch_log_event():
    """Suppress all log_event calls in unit tests.

    log_event requires a live asyncpg pool; unit tests use lightweight mocks
    that do not implement the full pool protocol.  Patching here prevents
    spurious TypeErrors from pool.acquire() returning a coroutine instead of
    an async context manager.

    Tests that specifically need to assert on log_event calls should override
    this patch within their own ``with patch(...)`` context.
    """
    with (
        patch(
            "telegram_bot.handlers.commands._auth.log_event",
            new_callable=AsyncMock,
        ),
        patch(
            "telegram_bot.handlers.commands.system_commands.log_event",
            new_callable=AsyncMock,
        ),
    ):
        yield
