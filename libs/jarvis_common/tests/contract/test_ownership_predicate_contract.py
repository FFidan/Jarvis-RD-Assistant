"""Predicate-direct contract tests for assert_paper_ownership (A251).

Tests call the helper directly with a real DB connection (contract_conn),
bypassing the HTTP layer. Covers the three access paths defined by the D4
canonical-corpus ownership decision.

Verified: libs/jarvis_common/jarvis_common/db_helpers.py:234-307 at HEAD.
Survivor-of (Phase C): per-handler IDOR ownership-mock tests in test_audit_idor_sweep.py.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


async def test_a251_assert_paper_ownership_owner_no_exception(contract_two_users, contract_conn):
    """A251: owner calling assert_paper_ownership on their own paper → no exception.

    Verified: db_helpers.py:289 — discovered_by == user_id fast-grant path.
    """
    from fastapi import HTTPException
    from jarvis_common.db_helpers import assert_paper_ownership

    # Should complete without raising
    try:
        await assert_paper_ownership(
            contract_conn,
            contract_two_users.paper_id_a,
            contract_two_users.user_a_id,
        )
    except HTTPException as exc:
        pytest.fail(f"Owner got unexpected HTTPException {exc.status_code}: {exc.detail}")


async def test_a251_assert_paper_ownership_non_owner_raises_403(contract_two_users, contract_conn):
    """A251: non-owner (not in user_library) calling assert_paper_ownership → 403.

    Verified: db_helpers.py:302-307 — user_library check fails → HTTPException(403).
    """
    from fastapi import HTTPException
    from jarvis_common.db_helpers import assert_paper_ownership

    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(
            contract_conn,
            contract_two_users.paper_id_a,
            contract_two_users.user_b_id,
        )
    assert exc_info.value.status_code == 403


async def test_a251_assert_paper_ownership_nonexistent_paper_raises_404(
    contract_conn,
):
    """A251: non-existent paper_id → 404.

    Verified: db_helpers.py:278 — fetchrow returns None → HTTPException(404).
    user_id is non-None so the single-user-mode early return does not fire.
    """
    from fastapi import HTTPException
    from jarvis_common.db_helpers import assert_paper_ownership

    nonexistent_id = 999_999_999

    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(contract_conn, nonexistent_id, user_id=1)
    assert exc_info.value.status_code == 404


async def test_a251_assert_paper_ownership_single_user_mode_skips_check(
    contract_two_users, contract_conn
):
    """A251: user_id=None (single-user mode) → no check, no exception for any paper.

    Verified: db_helpers.py:270-271 — early return when user_id is None.
    """
    from fastapi import HTTPException
    from jarvis_common.db_helpers import assert_paper_ownership

    try:
        # user B's paper accessed with user_id=None (single-user mode) — allowed
        await assert_paper_ownership(
            contract_conn,
            contract_two_users.paper_id_a,
            user_id=None,
        )
    except HTTPException as exc:
        pytest.fail(f"Single-user mode should skip ownership check, got {exc.status_code}")
