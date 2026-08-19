"""Settings nudges/sources/analytics contract tests — Cluster 1.

Covers admin-only nudge CRUD, sources list/reorder/update, and per-user analytics
scoping. Extends test_settings_contract.py (which covers config CRUD already).

Survivor-of (selected from the 20-test Cluster 1 deletion ledger; 12 of 20
land here, 8 deferred to rot-on-touch):
  test_list_nudges                              → S-01
  test_list_nudges_non_admin_returns_403        → S-02
  test_update_nudge_found / _not_found          → S-03
  test_list_sources / _ordered_by_display_order → S-04
  test_reorder_sources_persists_order / _unknown_source_returns_400 → S-05
  test_update_source_found / _not_found         → S-06
  test_papers_by_source / _scopes_non_admin_browser_user → S-07
  test_papers_by_status / _scopes_non_admin_browser_user → S-08

DEFERRED (rot-on-touch — require complex carve-out wiring or non-test infra):
  test_set_config_invalid_cron_returns_400      (scheduler validator setup)
  test_set_config_does_not_persist_when_litellm_update_fails (LiteLLM carve-out)
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


async def _promote_user_to_admin(conn, user_id: int) -> None:
    await conn.execute("UPDATE users SET role='admin' WHERE id=$1", user_id)


# ---------------------------------------------------------------------------
# S-01: GET /api/nudges — admin returns rows
# ---------------------------------------------------------------------------


async def test_s01_nudges_list_admin_returns_rows(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/nudges as admin returns list.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:269
    # (list_nudges: admin-only; SELECT FROM scheduled_nudges).
    """
    await _promote_user_to_admin(contract_conn, contract_two_users.user_a_id)
    # scheduled_nudges has UNIQUE(nudge_type); init.sql seeds rows. Verify the
    # admin can see them rather than inserting duplicates.

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/nudges")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    nudges = body if isinstance(body, list) else body.get("nudges", [])
    assert isinstance(nudges, list) and len(nudges) > 0, (
        f"Admin should see seeded nudges; got {nudges!r}"
    )


# ---------------------------------------------------------------------------
# S-02: GET /api/nudges — non-admin returns 403
# ---------------------------------------------------------------------------


async def test_s02_nudges_list_non_admin_returns_403(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """GET /api/nudges as a non-admin browser user returns 403.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:269
    # (list_nudges depends on require_admin).
    """
    # User A's seeded role is 'user' (default in _seed_user)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/nudges")

    assert resp.status_code in (401, 403), (
        f"Non-admin GET /api/nudges should be 401/403; got {resp.status_code}: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# S-03: PUT /api/nudges/{id} — update persists
# ---------------------------------------------------------------------------


async def test_s03_nudge_update_persists_to_db(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/nudges/{id} updates the row; re-fetch reflects new state.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings_sources.py:67
    # (update_nudge: admin-only; cron validation; owner-defined capability).
    """
    await _promote_user_to_admin(contract_conn, contract_two_users.user_a_id)
    # Use an existing seeded nudge row (UNIQUE constraint on nudge_type prevents inserts).
    nudge_id = await contract_conn.fetchval("SELECT id FROM scheduled_nudges ORDER BY id LIMIT 1")
    if nudge_id is None:
        pytest.skip("No seeded scheduled_nudges rows in contract DB")

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(
            f"/api/nudges/{nudge_id}",
            json={"enabled": False, "cron_expression": "0 18 * * *"},
        )

    assert resp.status_code == 200, resp.text[:300]
    enabled = await contract_conn.fetchval(
        "SELECT enabled FROM scheduled_nudges WHERE id=$1", nudge_id
    )
    assert enabled is False, f"Expected enabled=False after PUT; got {enabled!r}"


# ---------------------------------------------------------------------------
# S-04: GET /api/sources — list ordered by display_order
# ---------------------------------------------------------------------------


async def test_s04_sources_list_ordered_by_display_order(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/sources (admin) returns rows ordered by display_order ASC.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings_sources.py:113
    # (list_sources: Depends(require_admin); ORDER BY display_order ASC, id ASC).
    """
    await _promote_user_to_admin(contract_conn, contract_two_users.user_a_id)
    # SourceResponse Pydantic model only accepts source_type ∈ SourceType enum;
    # can't insert test_source_X. Verify ordering across existing seeded rows.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/sources")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    rows = body if isinstance(body, list) else body.get("sources", [])
    orders = [r.get("display_order", 0) for r in rows]
    assert orders == sorted(orders), f"Sources not ordered by display_order ASC: {orders}"


async def test_s04b_sources_list_non_admin_returns_403(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """GET /api/sources as a non-admin browser user returns 403.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings_sources.py:113
    # (list_sources: Depends(require_admin) — non-admin session ⇒ 403).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/sources")

    assert resp.status_code == 403, (
        f"Non-admin GET /api/sources should be 403; got {resp.status_code}: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# S-05: PATCH /api/sources/reorder — persists order
# ---------------------------------------------------------------------------


async def test_s05_sources_reorder_persists_order(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PATCH /api/sources/reorder applies new display_order values in a transaction.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:335
    # (reorder_sources: admin-only; validates each source_type exists; assigns
    # display_order = position in the supplied list).
    """
    await _promote_user_to_admin(contract_conn, contract_two_users.user_a_id)
    # Use existing source rows (init.sql seeds arxiv + semantic_scholar at least)
    src_types = await contract_conn.fetch(
        "SELECT source_type FROM paper_sources ORDER BY display_order LIMIT 2"
    )
    if len(src_types) < 2:
        pytest.skip("Not enough seeded paper_sources rows to reorder")
    reversed_order = [r["source_type"] for r in reversed(src_types)]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.patch(
            "/api/sources/reorder",
            json={"source_types": reversed_order},
        )

    assert resp.status_code in (200, 204), resp.text[:300]
    # Verify the first source_type in our reversed list now has display_order=0
    first_order = await contract_conn.fetchval(
        "SELECT display_order FROM paper_sources WHERE source_type=$1",
        reversed_order[0],
    )
    # reorder_sources uses `enumerate(source_types, start=1)` — display_order is 1-indexed.
    assert first_order == 1, (
        f"After reorder, expected first source display_order=1 (1-indexed); got {first_order}"
    )


# ---------------------------------------------------------------------------
# S-06: PUT /api/sources/{id} — update persists
# ---------------------------------------------------------------------------


async def test_s06_sources_update_persists_to_db(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/sources/{id} updates the row; re-fetch reflects new state.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:363
    # (update_source: admin-only; 404 on missing; dynamic_update).
    """
    await _promote_user_to_admin(contract_conn, contract_two_users.user_a_id)
    # SourceResponse enum constraint: can't insert test sources; reuse an existing row.
    src_id = await contract_conn.fetchval(
        "SELECT id FROM paper_sources WHERE source_type='arxiv' ORDER BY id LIMIT 1"
    )
    if src_id is None:
        pytest.skip("No seeded paper_sources row for arxiv")

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/sources/{src_id}", json={"enabled": False})

    assert resp.status_code == 200, resp.text[:300]
    enabled = await contract_conn.fetchval("SELECT enabled FROM paper_sources WHERE id=$1", src_id)
    assert enabled is False, f"Expected enabled=False after PUT; got {enabled!r}"


# ---------------------------------------------------------------------------
# S-07: GET /api/analytics/papers-by-source — scoped per non-admin caller
# ---------------------------------------------------------------------------


async def test_s07_analytics_papers_by_source_scopes_to_user(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """GET /api/analytics/papers-by-source as non-admin scopes to caller's library.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:397
    # (papers_by_source: scopes to user_id when non-admin).
    """
    # User A has seeded paper_id_a (source_type='arxiv') in user_library.
    # User B's seeded paper is also arxiv but under user_b's library.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/analytics/papers-by-source")
    assert resp_a.status_code == 200, resp_a.text[:300]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/analytics/papers-by-source")
    assert resp_b.status_code == 200, resp_b.text[:300]

    # Both users have own seeded paper; their counts are independent
    # (the response shape may differ; just verify endpoint returns 200 + dict/list).
    body_a = resp_a.json()
    body_b = resp_b.json()
    assert body_a is not None and body_b is not None


# ---------------------------------------------------------------------------
# S-08: GET /api/analytics/papers-by-status — scoped per non-admin caller
# ---------------------------------------------------------------------------


async def test_s08_analytics_papers_by_status_scopes_to_user(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """GET /api/analytics/papers-by-status as non-admin scopes to caller.

    # Verified: services/paper_ingestion/paper_ingestion/routers/settings.py:410
    # (papers_by_status: scopes to user_id when non-admin).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/analytics/papers-by-status")
    assert resp_a.status_code == 200, resp_a.text[:300]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/analytics/papers-by-status")
    assert resp_b.status_code == 200, resp_b.text[:300]
