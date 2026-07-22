"""Real-PostgreSQL contracts for the central paper visibility guard.

Tests call the helper directly with a real DB connection (contract_conn),
bypassing the HTTP layer. They cover library membership, rejection outside the
library, missing rows, and the explicit trusted-internal bypass.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_a251_library_member_no_exception(contract_two_users, contract_conn):
    """A caller with explicit library membership passes the guard."""
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
        pytest.fail(f"Library member got HTTPException {exc.status_code}: {exc.detail}")


async def test_a251_private_paper_outside_library_raises_403(contract_two_users, contract_conn):
    """A private paper outside the caller's library is rejected with 403."""
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
    """A ``None`` caller preserves the explicit trusted-internal bypass."""
    from fastapi import HTTPException
    from jarvis_common.db_helpers import assert_paper_ownership

    try:
        # Trusted internal access intentionally bypasses end-user authorization.
        await assert_paper_ownership(
            contract_conn,
            contract_two_users.paper_id_a,
            user_id=None,
        )
    except HTTPException as exc:
        pytest.fail(f"Trusted internal mode should skip the check, got {exc.status_code}")


async def test_batch_guard_uses_the_same_visibility_policy(
    contract_two_users,
    contract_conn,
) -> None:
    """Batch authorization accepts public/library rows and rejects other private rows.

    The call uses real PostgreSQL rows so it proves the shared predicate's
    behavioral consequence, including duplicate input normalization and the
    precedence of a missing-row 404 over an unauthorized-row 403.
    """
    from fastapi import HTTPException
    from jarvis_common.db_helpers import assert_papers_ownership

    public_id = await contract_conn.fetchval(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url, visibility_scope
           ) VALUES (
               'ownership-batch-public', 'arxiv', 'Batch public', ARRAY['A'],
               'https://example.test/ownership-batch-public', 'public'
           ) RETURNING id"""
    )
    private_other_id = await contract_conn.fetchval(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url,
               discovered_by, visibility_scope
           ) VALUES (
               'ownership-batch-private', 'local', 'Batch private', ARRAY['A'],
               'https://example.test/ownership-batch-private', $1, 'private'
           ) RETURNING id""",
        contract_two_users.user_b_id,
    )

    await assert_papers_ownership(
        contract_conn,
        [public_id, contract_two_users.paper_id_a, public_id],
        contract_two_users.user_a_id,
    )
    with pytest.raises(HTTPException) as forbidden:
        await assert_papers_ownership(
            contract_conn,
            [public_id, private_other_id],
            contract_two_users.user_a_id,
        )
    assert forbidden.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        await assert_papers_ownership(
            contract_conn,
            [private_other_id, 999_999_999],
            contract_two_users.user_a_id,
        )
    assert missing.value.status_code == 404
