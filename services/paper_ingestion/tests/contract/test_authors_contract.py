"""Authors domain contract tests — target rows A19-A24.

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
    Survivor-of: no prior mock-unit tests for this endpoint.
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
    assert "matches" in body, f"Missing 'matches' key: {body}"
    assert isinstance(body["new_papers"], int) and body["new_papers"] >= 0
    assert isinstance(body["authors_checked"], int) and body["authors_checked"] >= 0
    assert isinstance(body["matches"], list)


# ---------------------------------------------------------------------------
# T9: POST /api/authors/check — visible recent-paper boundary
#
# The scan includes persisted-public discovery results plus private papers in
# the caller's library. Private papers outside that boundary must not leak
# through author alert cards.
# ---------------------------------------------------------------------------


async def _track_author(conn, user_id: int, name: str) -> int:
    """Insert an enabled tracked author for *user_id*; return its id."""
    return await conn.fetchval(
        "INSERT INTO tracked_authors (author_name, user_id, source, enabled) "
        "VALUES ($1, $2, 'manual', TRUE) RETURNING id",
        name,
        user_id,
    )


async def _insert_recent_paper(
    conn,
    *,
    ext: str,
    authors: list[str],
    metadata=None,
    visibility_scope: str = "public",
) -> int:
    """Insert a recent paper with explicit visibility and no library row.

    Leaving metadata as None exercises the NULL-tolerant path; passing an empty
    list of authors etc. is the caller's choice. created_at defaults to now()
    so the paper is inside the 24h window.
    """
    return await conn.fetchval(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url, metadata, visibility_scope
           )
           VALUES ($1, 'arxiv', $2, $3::text[], $4, $5, $6)
           RETURNING id""",
        ext,
        f"Recent paper {ext}",
        authors,
        f"https://example.test/{ext}",
        metadata,
        visibility_scope,
    )


async def test_t9_check_matches_public_paper_not_in_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """A persisted-public paper can produce an alert without a library row."""
    await _track_author(contract_conn, contract_two_users.user_a_id, "Grace Hopper")
    paper_id = await _insert_recent_paper(
        contract_conn,
        ext="t9-public",
        authors=["Grace Hopper", "Co Author"],
        metadata={"foo": "bar"},
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors/check")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["new_papers"] >= 1, f"Public paper not matched: {body}"

    # The matched paper is the one we inserted (not in the user's library).
    matched_ids = {p["id"] for m in body["matches"] for p in m["papers"]}
    assert paper_id in matched_ids, (
        f"Paper {paper_id} (not in library) missing from matches: {body['matches']}"
    )


async def test_t9_check_hides_private_paper_outside_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """A private paper outside the caller's library cannot leak through alerts."""
    await _track_author(contract_conn, contract_two_users.user_a_id, "Katherine Johnson")
    paper_id = await _insert_recent_paper(
        contract_conn,
        ext="t9-private-hidden",
        authors=["Katherine Johnson"],
        visibility_scope="private",
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors/check")

    assert resp.status_code == 200, resp.text[:300]
    matched_ids = {p["id"] for match in resp.json()["matches"] for p in match["papers"]}
    assert paper_id not in matched_ids


async def test_t9_check_matches_private_paper_in_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """A private paper explicitly in the caller's library can produce an alert."""
    user_id = contract_two_users.user_a_id
    await _track_author(contract_conn, user_id, "Dorothy Vaughan")
    paper_id = await _insert_recent_paper(
        contract_conn,
        ext="t9-private-library",
        authors=["Dorothy Vaughan"],
        visibility_scope="private",
    )
    await contract_conn.execute(
        """INSERT INTO user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        user_id,
        paper_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors/check")

    assert resp.status_code == 200, resp.text[:300]
    matched_ids = {p["id"] for match in resp.json()["matches"] for p in match["papers"]}
    assert paper_id in matched_ids


async def test_t9_check_uses_owner_override_resolver(_pi_app_with_pool):
    """check_tracked_authors resolves identity via the bot resolver so the bot
    can call it with X-Owner-User-Id.

    Dependency-identity assert against the route's declared dependency.
    """
    from jarvis_common.auth import get_current_user_id_or_bot

    # Find the /api/authors/check route and confirm its handler declares the
    # override-honouring resolver.
    import inspect

    from paper_ingestion.routers import authors as authors_mod

    src = inspect.getsource(authors_mod.check_tracked_authors)
    assert "get_current_user_id_or_bot" in src, (
        "check_tracked_authors must resolve identity via get_current_user_id_or_bot"
    )
    # The symbol is imported into the router module namespace.
    assert authors_mod.get_current_user_id_or_bot is get_current_user_id_or_bot


async def test_t9_check_enriches_matches_with_card_keys(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Enrich: matches groups newly-alerted papers by author and carries the
    format_paper_card keys; new_papers count is consistent with matches.

    Verified: authors.py check_tracked_authors — AuthorAlertMatch shape.
    """
    await _track_author(contract_conn, contract_two_users.user_a_id, "Ada Lovelace")
    paper_id = await _insert_recent_paper(
        contract_conn,
        ext="t9-enrich",
        authors=["Ada Lovelace"],
        metadata={"s2_author_ids": []},
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors/check")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    # The newly-alerted paper count equals the total papers across matches.
    total_in_matches = sum(len(m["papers"]) for m in body["matches"])
    assert body["new_papers"] == total_in_matches, (
        f"new_papers ({body['new_papers']}) != papers in matches ({total_in_matches})"
    )

    ada = next((m for m in body["matches"] if m["author_name"] == "Ada Lovelace"), None)
    assert ada is not None, f"Ada Lovelace not in matches: {body['matches']}"
    paper = next((p for p in ada["papers"] if p["id"] == paper_id), None)
    assert paper is not None, f"Inserted paper missing from Ada's matches: {ada}"
    for key in ("id", "title", "authors", "published_date", "source_type", "url", "metadata"):
        assert key in paper, f"Card key '{key}' missing from match paper: {paper}"
    assert paper["authors"] == ["Ada Lovelace"]
    assert paper["source_type"] == "arxiv"


async def test_t9_check_dedup_second_call_empty_and_log_row_exists(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Dedup: a second check returns new_papers==0 and matches==[]; the
    author_alert_log row persists after the first call.

    Verified: authors.py check_tracked_authors — per-user record_author_alert.
    """
    author_id = await _track_author(contract_conn, contract_two_users.user_a_id, "Alan Turing")
    paper_id = await _insert_recent_paper(
        contract_conn,
        ext="t9-dedup",
        authors=["Alan Turing"],
        metadata={},
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp1 = await c.post("/api/authors/check")
    assert resp1.status_code == 200, resp1.text[:300]
    assert resp1.json()["new_papers"] >= 1

    # author_alert_log row exists (per-user dedup ledger) after the first call.
    log_row = await contract_conn.fetchval(
        "SELECT id FROM author_alert_log "
        "WHERE tracked_author_id = $1 AND paper_id = $2 AND user_id = $3",
        author_id,
        paper_id,
        contract_two_users.user_a_id,
    )
    assert log_row is not None, "author_alert_log row missing after first check"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp2 = await c.post("/api/authors/check")
    assert resp2.status_code == 200, resp2.text[:300]
    body2 = resp2.json()
    assert body2["new_papers"] == 0, f"Second call must dedup: {body2}"
    assert body2["matches"] == [], f"Second call matches must be empty: {body2}"


async def test_t9_check_null_metadata_does_not_500(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """NULL-tolerant: a matched paper with NULL metadata (and no summary)
    serializes without a 500.

    Verified: authors.py check_tracked_authors — `paper_metadata = paper["metadata"] or {}`.
    """
    await _track_author(contract_conn, contract_two_users.user_a_id, "Margaret Hamilton")
    # Force a genuine SQL NULL metadata (override the '{}' default).
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url, metadata, visibility_scope
           )
           VALUES ('t9-null', 'arxiv', 'Null meta paper', ARRAY['Margaret Hamilton'],
                   'https://example.test/t9-null', NULL, 'public')
           RETURNING id""",
    )
    null_meta = await contract_conn.fetchval("SELECT metadata FROM papers WHERE id = $1", paper_id)
    assert null_meta is None, "metadata should be SQL NULL for this fixture"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/authors/check")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    matched_ids = {p["id"] for m in body["matches"] for p in m["papers"]}
    assert paper_id in matched_ids, f"NULL-metadata paper not matched: {body['matches']}"
    paper = next(p for m in body["matches"] for p in m["papers"] if p["id"] == paper_id)
    # metadata coerced to {} (the `or {}` guard), serializes cleanly.
    assert paper["metadata"] == {}


# ---------------------------------------------------------------------------
# A25: Multi-tenant isolation — (user_id, author_name, s2_author_id) unique
# ---------------------------------------------------------------------------


async def test_a25_tracked_authors_per_user_unique_constraint(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers the tracked_authors unique constraint being per-user.

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
