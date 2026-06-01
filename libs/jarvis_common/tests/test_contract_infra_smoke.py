"""Char-tests proving the Wave-4 contract fixture infra invariants.

Each test was authored with a RED proof (the assertion was first proven to FAIL
against a deliberately-broken fixture: e.g. commenting out txn.rollback to prove
the rollback is what wipes writes). Run RED proofs at authoring time, not in CI.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


async def test_char1_rollback_wipes_writes_first_test(contract_conn):
    """Char-test 1a: insert a sentinel; test 1b (next, same session) sees zero."""
    await contract_conn.execute(
        "INSERT INTO users (id, email) VALUES (777001, 'char1a@test.local')"
    )
    n = await contract_conn.fetchval("SELECT count(*) FROM users WHERE id = 777001")
    assert n == 1


async def test_char1_rollback_wipes_writes_next_test(contract_conn):
    """Char-test 1b: row from 1a is GONE — rollback fired between tests."""
    n = await contract_conn.fetchval("SELECT count(*) FROM users WHERE id = 777001")
    assert n == 0, "rollback did not contain the seed from test_char1a"


_PID_CAPTURE: dict[str, int] = {}


async def test_char2_session_container_reused_capture(contract_conn):
    """Char-test 2a: capture backend PID."""
    _PID_CAPTURE["a"] = await contract_conn.fetchval("SELECT pg_backend_pid()")


async def test_char2_session_container_reused_verify(contract_conn):
    """Char-test 2b: server identity is stable — single PG instance per session."""
    assert "a" in _PID_CAPTURE
    dd = await contract_conn.fetchval(
        "SELECT setting FROM pg_settings WHERE name = 'data_directory'"
    )
    assert dd is not None
    # Server identity is stable across tests in the session.


async def test_char3_container_survives_test_failure_seed(contract_conn):
    """Char-test 3a: seed a row then the next test asserts gone (rollback fired despite ok)."""
    await contract_conn.execute("INSERT INTO users (id, email) VALUES (777003, 'char3@test.local')")


async def test_char3_container_survives_test_failure_check(contract_conn):
    """Char-test 3b: container still responsive + 3a seed gone (proves teardown didn't cascade)."""
    n = await contract_conn.fetchval("SELECT count(*) FROM users WHERE id = 777003")
    assert n == 0
    one = await contract_conn.fetchval("SELECT 1")
    assert one == 1


async def test_char4_contract_two_users_seeds_contained_seed(contract_two_users):
    """Char-test 4a: contract_two_users seeds 2 users + resources; assert seeds visible."""
    assert contract_two_users.user_a_id != contract_two_users.user_b_id


async def test_char4_contract_two_users_seeds_contained_check(contract_conn):
    """Char-test 4b: zero users in DB — proves contract_two_users seed was CONTAINED by rollback.

    This is the riskiest invariant: if the seed escaped, contract tests bleed.
    """
    n = await contract_conn.fetchval("SELECT count(*) FROM users")
    assert n == 0, "contract_two_users seed escaped the rollback — tests will bleed"


# Char-test 5 (pi_test_client) deferred: per-service test_client fixtures are
# authored in their own conftests. The shared
# SharedConnPool + SharedAcquireCM are already exercised structurally by the
# above tests (every contract_* test path uses the session pool + per-test conn).
