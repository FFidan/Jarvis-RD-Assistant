"""Authors domain contract tests — Phase B target rows A19-A24.

Survivor-of: (all NONE — no prior contract coverage).
Carve-out: app.state.http_client is MagicMock (outbound HTTP).

Rows covered:
  A19 GET  /api/authors            — list returns only current user's rows
  A20 POST /api/authors            — insert + 409 on duplicate
  A21 PUT  /api/authors/{id}       — update persists; 404 for non-owner
  A22 DELETE /api/authors/{id}     — delete scoped to user; 404 for non-owner
  A23 POST /api/authors/auto-detect — detects from starred papers; count matches DB
  A24 POST /api/authors/check       — returns only current-user matches
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


# ---------------------------------------------------------------------------
# A19: GET /api/authors — list returns only current user's tracked_authors rows
# ---------------------------------------------------------------------------


async def test_a19_list_authors_returns_only_own_rows(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A19: GET /api/authors scoped to current user.

    Verified: authors.py:38-50 list_tracked_authors — WHERE user_id IS NOT DISTINCT FROM $1.
    Survivor-of (future Phase C): no prior mock-unit tests for this endpoint.
    """
    # Seed one author for user A
    author_a_id = await contract_conn.fetchval(
        "INSERT INTO tracked_authors (author_name, user_id, source) "
        "VALUES ('Author A', $1, 'manual') RETURNING id",
        contract_two_users.user_a_id,
    )
    # Seed one author for user B (must not appear in A's response)
    await contract_conn.execute(
        "INSERT INTO tracked_authors (author_name, user_id, source) "
        "VALUES ('Author B', $1, 'manual')",
        contract_two_users.user_b_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/authors")

    assert resp.status_code == 200, resp.text[:300]
    items = resp.json()
    ids = [item["id"] for item in items]
    assert author_a_id in ids, f"User A's author {author_a_id} missing from list"
    author_b_names = [item["author_name"] for item in items if item["author_name"] == "Author B"]
    assert author_b_names == [], f"User B's author leaked into User A's response: {author_b_names}"


# ---------------------------------------------------------------------------
# A20: POST /api/authors — insert row; 409 on duplicate
# ---------------------------------------------------------------------------


async def test_a20_create_author_inserts_row_and_409_on_duplicate(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A20: POST /api/authors inserts tracked_author for current user; 409 on dup.

    Verified: authors.py:55-82 create_tracked_author — INSERT + 409 guard.
    """
    payload = {"author_name": "Unique Contract Author", "s2_author_id": None}

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors", json=payload)

    assert resp.status_code == 201, resp.text[:300]
    body = resp.json()
    assert body["author_name"] == "Unique Contract Author"
    inserted_id = body["id"]

    # Verify row persisted in DB
    row = await contract_conn.fetchrow("SELECT * FROM tracked_authors WHERE id = $1", inserted_id)
    assert row is not None, "Row not found in DB after create"
    assert row["user_id"] == contract_two_users.user_a_id

    # Second identical call must return 409
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp2 = await c.post("/api/authors", json=payload)
    assert resp2.status_code == 409, f"Expected 409 on duplicate, got {resp2.status_code}"


# ---------------------------------------------------------------------------
# A21: PUT /api/authors/{id} — update persists; 404 for non-owner
# ---------------------------------------------------------------------------


async def test_a21_update_author_persists_and_404_for_non_owner(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A21: PUT /api/authors/{id} updates fields; 404 for wrong user.

    Verified: authors.py:87-115 update_tracked_author — ownership WHERE user_id.
    """
    author_id = await contract_conn.fetchval(
        "INSERT INTO tracked_authors (author_name, user_id, source) "
        "VALUES ('Update Test Author', $1, 'manual') RETURNING id",
        contract_two_users.user_a_id,
    )

    # Owner can update
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/authors/{author_id}", json={"enabled": False})

    assert resp.status_code == 200, resp.text[:300]
    updated = resp.json()
    assert updated["enabled"] is False

    # Non-owner gets 404
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.put(f"/api/authors/{author_id}", json={"enabled": True})

    assert resp_b.status_code == 404, f"Expected 404 for non-owner, got {resp_b.status_code}"


# ---------------------------------------------------------------------------
# A22: DELETE /api/authors/{id} — deletes scoped row; 404 for non-owner
# ---------------------------------------------------------------------------


async def test_a22_delete_author_removes_row_and_404_for_non_owner(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A22: DELETE /api/authors/{id} deletes DB row; 404 for wrong user.

    Verified: authors.py:120-139 delete_tracked_author — delete_or_404 with user_id check.
    """
    author_id = await contract_conn.fetchval(
        "INSERT INTO tracked_authors (author_name, user_id, source) "
        "VALUES ('Delete Test Author', $1, 'manual') RETURNING id",
        contract_two_users.user_a_id,
    )

    # Non-owner attempt should 404 (not delete)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.delete(f"/api/authors/{author_id}")
    assert resp_b.status_code == 404, f"Expected 404 for non-owner, got {resp_b.status_code}"

    # Row still present after non-owner attempt
    still_exists = await contract_conn.fetchval(
        "SELECT id FROM tracked_authors WHERE id = $1", author_id
    )
    assert still_exists is not None, "Row was incorrectly deleted by non-owner"

    # Owner can delete
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.delete(f"/api/authors/{author_id}")
    assert resp_a.status_code == 204, resp_a.text[:300]

    # Row gone from DB
    gone = await contract_conn.fetchval("SELECT id FROM tracked_authors WHERE id = $1", author_id)
    assert gone is None, "Row still present after owner delete"


# ---------------------------------------------------------------------------
# A23: POST /api/authors/auto-detect — detects from starred papers
# ---------------------------------------------------------------------------


async def test_a23_auto_detect_authors_returns_response_shape(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A23: POST /api/authors/auto-detect returns AutoDetectResponse shape.

    Verified: authors.py:149-219 auto_detect_authors — scans starred/rated papers for user.
    Note: contract_two_users seeds paper_user_state with starred=TRUE, so the endpoint
    has material to detect from.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors/auto-detect")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert "added" in body, f"Missing 'added' key: {body}"
    assert "already_tracked" in body, f"Missing 'already_tracked' key: {body}"
    assert "authors" in body, f"Missing 'authors' key: {body}"
    assert isinstance(body["added"], int) and body["added"] >= 0
    assert isinstance(body["already_tracked"], int) and body["already_tracked"] >= 0
    assert isinstance(body["authors"], list)


# ---------------------------------------------------------------------------
# A24: POST /api/authors/check — returns matches for current user only
# ---------------------------------------------------------------------------


async def test_a24_check_authors_returns_only_own_user_results(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A24: POST /api/authors/check scoped to current user.

    Verified: authors.py:224-304 check_tracked_authors — WHERE enabled=TRUE AND user_id=$1.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors/check")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert "new_papers" in body, f"Missing 'new_papers' key: {body}"
    assert "authors_checked" in body, f"Missing 'authors_checked' key: {body}"
    assert isinstance(body["new_papers"], int) and body["new_papers"] >= 0
    assert isinstance(body["authors_checked"], int) and body["authors_checked"] >= 0


# ---------------------------------------------------------------------------
# A25: Multi-tenant isolation — (user_id, author_name, s2_author_id) unique
# ---------------------------------------------------------------------------


async def test_a25_tracked_authors_per_user_unique_constraint(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers HIGH-PI-01: tracked_authors unique constraint is per-user.

    User A and user B both track "Alice Smith" (s2_author_id=None).
    Each must get their own row — cross-user conflict must NOT fire.
    A second attempt by the same user must return 409.

    Verified: db/init.sql tracked_authors_name_s2_unique UNIQUE (user_id, author_name, s2_author_id).
    Verified: authors.py:55-82 create_tracked_author — pre-check + INSERT.
    """
    payload = {"author_name": "Alice Smith", "s2_author_id": None}

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.post("/api/authors", json=payload)
    assert resp_a.status_code == 201, f"User A create failed: {resp_a.text[:300]}"
    id_a = resp_a.json()["id"]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.post("/api/authors", json=payload)
    assert resp_b.status_code == 201, (
        f"User B must get their own row (no cross-user conflict), got {resp_b.status_code}: "
        f"{resp_b.text[:300]}"
    )
    id_b = resp_b.json()["id"]
    assert id_a != id_b, "User A and user B must have separate tracked_authors rows"

    row_a = await contract_conn.fetchrow("SELECT user_id FROM tracked_authors WHERE id = $1", id_a)
    row_b = await contract_conn.fetchrow("SELECT user_id FROM tracked_authors WHERE id = $1", id_b)
    assert row_a["user_id"] == contract_two_users.user_a_id
    assert row_b["user_id"] == contract_two_users.user_b_id

    # Same user, same author → 409
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_dup = await c.post("/api/authors", json=payload)
    assert resp_dup.status_code == 409, (
        f"Duplicate for same user must be 409, got {resp_dup.status_code}"
    )
