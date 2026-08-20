"""Priority domain contract tests — target rows A91, A92.

Survivor-of: test_priority.py mock-unit assertions for compute_paper_priority,
    recompute_all_priorities.
Carve-out: require_admin bypassed for A92 (admin-gated recompute endpoint).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    patch_pi_test_app,
)
from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_admin(contract_conn):
    """PI app + require_admin bypassed for recompute-priorities (admin-gated)."""
    from jarvis_common.auth import require_admin
    from jarvis_common.testing_auth import SignedIdentityMiddleware
    from jarvis_common.testing_db import SharedConnPool
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    async def _allow_admin():
        return None

    shared = SharedConnPool(
        contract_conn,
        session_authorization="jarvis_research_runtime",
    )
    with patch_pi_test_app(
        shared,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=True,
            dependency_overrides={require_admin: _allow_admin},
        ),
    ) as wired_app:
        yield SignedIdentityMiddleware(
            wired_app,
            audience="research",
            session_pool=shared.with_session_authorization("jarvis_platform_runtime"),
        )


# ---------------------------------------------------------------------------
# A91: POST /api/papers/{paper_id}/priority — priority score written to DB
# ---------------------------------------------------------------------------


async def test_a91_compute_priority_writes_score_to_db(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A91: POST /api/papers/{id}/priority writes priority_score to papers row.

    Verified: priority.py:25-68 compute_paper_priority at HEAD d21aaea8.
    Survivor-of: test_priority.py mock-unit tests.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/papers/{paper_id}/priority")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "priority_score" in body, f"Missing priority_score in response: {body}"
    assert "priority_level" in body, f"Missing priority_level in response: {body}"
    assert body["paper_id"] == paper_id

    # Verify DB updated
    row = await contract_conn.fetchrow(
        "SELECT priority_score FROM papers WHERE id = $1",
        paper_id,
    )
    assert row is not None
    assert row["priority_score"] is not None, "priority_score must be written to papers table"
    assert abs(row["priority_score"] - body["priority_score"]) < 0.001, (
        f"DB priority_score={row['priority_score']} != response {body['priority_score']}"
    )


async def test_a91_compute_priority_user_b_gets_403_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A91 (PARTIAL-IDOR): user B denied priority compute on user A's paper.

    Verified: priority.py:44 assert_paper_ownership at HEAD d21aaea8.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.post(f"/api/papers/{paper_id}/priority")

    assert resp.status_code in (403, 404), (
        f"User B should get 403/404 for user A's paper priority; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# A92: POST /api/papers/recompute-priorities — updates all papers for user
# ---------------------------------------------------------------------------


async def test_a92_recompute_all_priorities_returns_updated_count(
    contract_two_users,
    _pi_app_admin,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A92: POST /api/papers/recompute-priorities returns updated count.

    Verified: priority.py:71-112 recompute_all_priorities at HEAD d21aaea8.
    Survivor-of: test_priority.py mock-unit tests.
    """
    # Seed a second paper to ensure there are rows to update
    await contract_conn.execute(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('priority-recompute-ext', 'arxiv', 'Recompute Test Paper',
                   ARRAY['Author'], 'https://priority.test/recompute',
                   $1)
           ON CONFLICT (external_id) DO NOTHING""",
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_admin, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/papers/recompute-priorities")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "updated" in body, f"Missing 'updated' field in response: {body}"
    assert isinstance(body["updated"], int) and body["updated"] >= 0, (
        f"'updated' must be non-negative int; got {body['updated']!r}"
    )
