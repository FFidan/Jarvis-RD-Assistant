"""Contract tests for the post-pool boot validator against a real database.

Complements the mocked unit tests in ``test_jarvis_common_auth.py`` by running
``validate_runtime_config``'s own user-count and admin-count SQL against
PostgreSQL, so a regression in either query (for example a flipped predicate
that fails the multi-user model-signing gate open) is caught here rather than
slipping past the mocks.
"""

from __future__ import annotations

import pytest

from jarvis_common.auth import validate_runtime_config
from jarvis_common.testing import SharedConnPool, seed_user_row

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_runtime_config_multi_user_without_hmac_raises(contract_conn):
    await seed_user_row(contract_conn, "owner@example.com", "admin")
    await seed_user_row(contract_conn, "member@example.com", "user")
    with pytest.raises(RuntimeError, match="JARVIS_MODEL_HMAC_KEY"):
        await validate_runtime_config(
            SharedConnPool(contract_conn),
            environment="development",
            setup_token_set=True,
            model_hmac_ok=False,
        )


async def test_runtime_config_single_user_without_hmac_ok(contract_conn):
    await seed_user_row(contract_conn, "solo@example.com", "admin")
    await validate_runtime_config(
        SharedConnPool(contract_conn),
        environment="development",
        setup_token_set=True,
        model_hmac_ok=False,
    )


async def test_runtime_config_multi_user_with_hmac_ok(contract_conn):
    await seed_user_row(contract_conn, "owner@example.com", "admin")
    await seed_user_row(contract_conn, "member@example.com", "user")
    await validate_runtime_config(
        SharedConnPool(contract_conn),
        environment="development",
        setup_token_set=True,
        model_hmac_ok=True,
    )


async def test_runtime_config_prod_no_admin_no_token_raises(contract_conn):
    await seed_user_row(contract_conn, "member@example.com", "user")
    with pytest.raises(RuntimeError, match="JARVIS_SETUP_TOKEN"):
        await validate_runtime_config(
            SharedConnPool(contract_conn),
            environment="production",
            setup_token_set=False,
            model_hmac_ok=True,
        )
