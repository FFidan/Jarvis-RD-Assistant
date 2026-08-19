"""Contract-test fixtures for the Platform API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from jarvis_common.db_helpers import init_pg_connection
from jarvis_common.testing import (
    SharedConnPool,
    _make_contract_conn_fixture,
    _make_contract_pool_fixture,
    _make_contract_two_users_fixture,
    make_contract_pg_dsn,
)
from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    configure_contract_api_key,
    patch_pi_test_app,
)

contract_pg_dsn = make_contract_pg_dsn("jarvis-platform-contract")
_contract_pool = _make_contract_pool_fixture()
contract_conn = _make_contract_conn_fixture()
contract_two_users = _make_contract_two_users_fixture()


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Configure the standard API key for Platform contract clients."""
    with configure_contract_api_key(monkeypatch) as key:
        yield key


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _platform_app_with_pool(contract_conn: Any) -> AsyncIterator[Any]:
    """Wire the real Platform app to the rollback-scoped connection."""
    from platform_api.deps import get_db_pool, limiter
    from platform_api.main import app as platform_app

    shared = SharedConnPool(contract_conn, session_authorization="jarvis_platform_runtime")
    with patch_pi_test_app(
        shared,
        app=platform_app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(remove_identity_overrides=True),
    ) as app:
        yield app


PLATFORM_RUNTIME_PASSWORD = "platform-runtime-contract-password"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def platform_runtime_conn(contract_pg_dsn: str, _contract_pool: Any) -> AsyncIterator[Any]:
    """Yield a connection authenticated as the platform runtime role.

    Erasure and account state changed through owner-defined capabilities that
    check the calling role, so a contract exercising those paths has to connect
    as the role production uses rather than as the privileged bootstrap login.
    """
    bootstrap = await asyncpg.connect(contract_pg_dsn)
    await init_pg_connection(bootstrap)
    await bootstrap.execute(
        "ALTER ROLE jarvis_platform_runtime LOGIN PASSWORD 'platform-runtime-contract-password'"
    )
    runtime = await asyncpg.connect(
        contract_pg_dsn,
        user="jarvis_platform_runtime",
        password=PLATFORM_RUNTIME_PASSWORD,
    )
    await init_pg_connection(runtime)
    transaction = runtime.transaction()
    await transaction.start()
    try:
        yield runtime
    finally:
        await transaction.rollback()
        await runtime.close()
        await bootstrap.close()
