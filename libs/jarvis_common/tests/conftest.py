"""Shared test fixtures for jarvis_common tests.

Infrastructure helpers (live_pg_dsn) are re-exported from
jarvis_common.testing so that the fixture is consistent across services
(--import-mode=importlib + shared tests namespace invariant).
"""

from __future__ import annotations

import pytest

# live_pg_dsn fixture for this library uses the "jarvis-rd" container prefix.
from jarvis_common.testing import make_live_pg_dsn as _make_live_pg_dsn

live_pg_dsn = _make_live_pg_dsn("jarvis-rd")

# Contract-layer fixtures (Wave 4): session-scoped Postgres + per-test txn rollback
from jarvis_common.testing import (  # noqa: E402, F401
    _make_contract_conn_fixture,
    _make_contract_pool_fixture,
    _make_contract_two_users_fixture,
)
from jarvis_common.testing import make_contract_pg_dsn as _make_contract_pg_dsn  # noqa: E402
from jarvis_common.testing_contract_apps import configure_contract_api_key  # noqa: E402

contract_pg_dsn = _make_contract_pg_dsn("jarvis-jc-contract")
_contract_pool = _make_contract_pool_fixture()
contract_conn = _make_contract_conn_fixture()
contract_two_users = _make_contract_two_users_fixture()


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    """Configure the standard contract-test API key for common contract tests."""
    with configure_contract_api_key(monkeypatch) as key:
        yield key
