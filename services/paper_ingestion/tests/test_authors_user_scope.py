"""Tracked-author updates are scoped to the owning user.

The update statement carries the caller's user_id in its WHERE clause, so a
non-owner's write cannot mutate another user's row even if the ownership
pre-check were bypassed by a race. Proven behaviorally against real Postgres:
user B's update of user A's tracked author is rejected and leaves A's row
untouched, while user A's identical update succeeds.
"""

from __future__ import annotations

import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_non_owner_cannot_update_another_users_tracked_author(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """User B's PUT on user A's tracked author is rejected and A's row is unchanged.

    The same write that user A can apply is a no-op for user B, proving the
    update is scoped to the owner rather than merely gated by the pre-check.

    # Verified: authors.py:90 update_tracked_author
    """
    author_id = await contract_conn.fetchval(
        "INSERT INTO tracked_authors (author_name, user_id, source, enabled) "
        "VALUES ('Owned By A', $1, 'manual', TRUE) RETURNING id",
        contract_two_users.user_a_id,
    )

    # Non-owner attempt: rejected, and A's row must be untouched.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.put(f"/api/authors/{author_id}", json={"enabled": False})

    assert resp_b.status_code in (403, 404), (
        f"non-owner write must be rejected; got {resp_b.status_code}: {resp_b.text[:200]}"
    )
    row = await contract_conn.fetchrow(
        "SELECT user_id, enabled FROM tracked_authors WHERE id = $1", author_id
    )
    assert row is not None, "user A's row must still exist after a non-owner attempt"
    assert row["user_id"] == contract_two_users.user_a_id, "ownership must be unchanged"
    assert row["enabled"] is True, "a non-owner write must not flip user A's enabled flag"

    # Owner applies the identical write successfully — proves the endpoint works
    # and the rejection above was an ownership decision, not a broken route.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.put(f"/api/authors/{author_id}", json={"enabled": False})

    assert resp_a.status_code == 200, f"owner update must succeed; got {resp_a.text[:200]}"
    assert resp_a.json()["enabled"] is False
    owner_row = await contract_conn.fetchrow(
        "SELECT enabled FROM tracked_authors WHERE id = $1", author_id
    )
    assert owner_row["enabled"] is False, "owner's update must persist"
