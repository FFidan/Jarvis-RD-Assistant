"""papers domain contract tests.

Covers endpoints NOT included in the shared IDOR quadruples at
libs/jarvis_common/tests/contract/test_idor_contract.py:

- GET /api/papers/brief — user_library scoping (behavioral, replaces SQL-substring
  assertions in test_audit_idor_sweep.py that only checked SQL structure)
- POST /api/papers/process_batch — ownership enforced before enqueue
  (not in IDOR quadruples since it's a POST acting on multiple paper_ids)
- PUT /api/papers/{id}/annotations — 200 response shape for owner (supplements
  the IDOR contract's 403/404 assertion for non-owner)

All tests require JARVIS_RUN_LIVE_PG=1 and run under -m contract.

Verified identifiers:
  papers.py:807 — annotate_paper calls assert_paper_ownership; RETURNING full user_state shape
  papers.py:~850+ — process_batch iterates paper_ids, calls assert_paper_ownership each
  papers.py:list_papers_brief — JOINs user_library for scoping
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    """paper_ingestion app wired to the contract conn pool.

    Removes the autouse ``current_user_id_strict_with_owner_override`` override so
    that session-cookie auth works.  Forces embedder=None so list_papers takes the
    BM25 path instead of the hybrid Qdrant path.
    """
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_contract_apps import patch_app_state, patch_dependency_overrides
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(app, {"db_pool": shared, "embedder": None}),
        patch_dependency_overrides(
            app, remove_overrides={current_user_id_strict_with_owner_override}
        ),
    ):
        yield app


# ---------------------------------------------------------------------------
# GET /api/papers/brief — user_library scoping (behavioral)
#
# Replaces the SQL-substring assertions in:
#   test_audit_idor_sweep.py::test_papers_brief_idor_user_id_filter_no_search
#   test_audit_idor_sweep.py::test_papers_brief_idor_user_id_filter_with_search
#
# Those tests verify "JOIN user_library" appears in the SQL string.
# This contract test verifies the BEHAVIORAL consequence: user A can see their
# own paper in the brief list; user B sees an empty list for the same paper.
#
# Note: the SQL-substring tests in test_audit_idor_sweep.py are kept alongside
# this contract test because they catch structural regressions (SQL changes)
# faster than the contract test (which requires JARVIS_RUN_LIVE_PG=1).
# ---------------------------------------------------------------------------


async def test_papers_brief_user_a_sees_own_paper(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/papers/brief: user A sees the paper they own in the brief list."""
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert contract_two_users.paper_id_a in ids, (
        f"paper_id_a={contract_two_users.paper_id_a} not in user A's brief list: {ids}"
    )


async def test_papers_brief_user_b_does_not_see_user_a_paper(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/papers/brief: user B does NOT see user A's paper in the brief list.

    This is the behavioral assertion that replaces
    test_audit_idor_sweep.py::test_papers_brief_idor_user_id_filter_no_search
    (SQL-substring: assert "JOIN user_library" in sql).
    The contract test asserts the observable effect: user B cannot enumerate
    user A's library via the brief endpoint.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert contract_two_users.paper_id_a not in ids, (
        f"user B should NOT see user A's paper {contract_two_users.paper_id_a} "
        f"in the brief list — IDOR leak: {ids}"
    )


# ---------------------------------------------------------------------------
# PUT /api/papers/{id}/annotations — 200 response shape for owner
#
# The IDOR contract (test_idor_contract.py quadruple:
#   ("PUT", "/api/papers/{paper_id_a}/annotations", "paper_id_a", "mutate"))
# asserts non-owner → 403/404. This test asserts the OWNER gets 200 with the
# correct response shape (state/starred/rating/user_notes/flagged/updated_at).
# ---------------------------------------------------------------------------


async def test_annotations_owner_gets_200_with_correct_shape(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """PUT /api/papers/{id}/annotations: owner gets 200 with user_state shape.

    Supplements the IDOR contract by verifying the owner-path response shape:
    the returned object must carry rating, user_notes, flagged, state, starred.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(
            f"/api/papers/{paper_id}/annotations",
            json={"rating": 4, "user_notes": "contract-test-note"},
        )

    assert resp.status_code == 200, f"Owner expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    # Response must carry all user_state fields per papers.py annotate_paper RETURNING clause
    for field in ("rating", "user_notes", "flagged", "state", "starred"):
        assert field in body, f"Missing field {field!r} in annotations response: {body}"
    assert body["rating"] == 4
    assert body["user_notes"] == "contract-test-note"


# ---------------------------------------------------------------------------
# behavioral replacements for SQL-substring/param-binding tests
#
# Each test below replaces 1-N deleted _make_pool_and_conn tests whose primary
# assertion was a SQL-text substring or positional-parameter index check.
# ---------------------------------------------------------------------------


# --- GET /api/papers — view / search / filter behavioral tests ---


async def test_list_papers_scoped_to_user_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/papers: user A only sees their own papers (user_library scoping).

    Replaces test_list_papers_no_filters_uses_limit_offset (SQL: JOIN user_library
    ul + ul.user_id=$1 + LIMIT/OFFSET param indices). The real query is executed
    against a live DB; if the JOIN or param binding is broken, user A's paper
    disappears from the response.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert contract_two_users.paper_id_a in ids, (
        f"paper_id_a={contract_two_users.paper_id_a} not in user A's list: {ids}"
    )
    # TwoUsers only exposes paper_id_a (user B's paper is seeded but not exposed as an attribute).
    # The scoping assertion is: user A's list must contain paper_id_a (the positive case).
    # Cross-user isolation is verified separately by test_papers_brief_user_b_does_not_see_user_a_paper.


async def test_list_papers_view_inbox_returns_real_inbox_papers(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """GET /api/papers?view=inbox: returns papers whose state is 'inbox' for this user.

    Replaces test_list_papers_with_view_inbox_uses_state_predicate (SQL: LEFT JOIN
    paper_user_state + COALESCE(pus.state,'inbox')='inbox'). Seeds a second paper
    in 'inbox' state; the seeded paper fixture is in 'to_read', so view=inbox
    should return the new paper but NOT the 'to_read' paper.
    """
    # Seed a paper owned by user A with no paper_user_state row (defaults to inbox)
    inbox_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'Inbox Test Paper', ARRAY['Author'], 'https://inbox.test/inbox', $2)
           RETURNING id""",
        "b109-inbox-test-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        inbox_paper_id,
    )
    # No paper_user_state row → COALESCE defaults to 'inbox'

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers", params={"view": "inbox"})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert inbox_paper_id in ids, f"inbox paper {inbox_paper_id} not in view=inbox list: {ids}"
    # The seeded fixture paper (state='to_read') must NOT appear in inbox view
    assert contract_two_users.paper_id_a not in ids, (
        f"to_read paper {contract_two_users.paper_id_a} incorrectly appears in inbox view: {ids}"
    )


async def test_list_papers_bm25_search_returns_matching_papers(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/papers?q=...: BM25 search returns papers matching the query.

    Replaces test_list_papers_search_query_uses_bm25_clause (SQL: search_vector @@
    plainto_tsquery). Uses the A_PAPER_TITLE sentinel ('Quantum Entanglement of Owls')
    which is indexed in paper A's title; a query for 'Quantum' must return it.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers", params={"q": "Quantum"})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert contract_two_users.paper_id_a in ids, (
        f"BM25 search for 'Quantum' should return paper_id_a={contract_two_users.paper_id_a}; "
        f"got: {ids}"
    )


async def test_list_papers_topic_filter_scopes_to_topic(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """GET /api/papers?topic_id=N: only papers in that topic are returned.

    Replaces test_list_papers_topic_filter_correct_param_indices (SQL: pt.topic_id=$1,
    ul.user_id=$2, LIMIT $3, OFFSET $4). Seeds a paper-topic link; the topic-filtered
    query must return the linked paper and not others.
    """
    topic_id = contract_two_users.topic_id_a
    paper_id = contract_two_users.paper_id_a

    # Ensure the paper is linked to the topic (fixture may or may not do this)
    await contract_conn.execute(
        """INSERT INTO paper_topics (paper_id, topic_id)
           VALUES ($1, $2)
           ON CONFLICT DO NOTHING""",
        paper_id,
        topic_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers", params={"topic_id": topic_id})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert paper_id in ids, f"paper_id_a={paper_id} not returned for topic_id={topic_id}: {ids}"


async def test_list_papers_view_source_type_combined_filter(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/papers?view=reading_list&source_type=arxiv: combined filter returns correct subset.

    Replaces test_list_papers_view_and_source_type_correct_param_indices (SQL:
    $1=user_id library join, $2=user_id pus join, $3=source_type, $4/$5=LIMIT/OFFSET).
    The seeded paper is source_type=arxiv and state=to_read (view='reading_list'); both filters pass.
    Note: the DB state value is 'to_read' but the API view name is 'reading_list'
    (VIEW_PREDICATES key, not the state enum value).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers", params={"view": "reading_list", "source_type": "arxiv"})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert contract_two_users.paper_id_a in ids, (
        f"paper_id_a={contract_two_users.paper_id_a} not returned for view=reading_list + "
        f"source_type=arxiv: {ids}"
    )
    # All returned papers must be arxiv (source_type filter is honoured)
    for p in resp.json():
        assert p.get("source_type") == "arxiv", (
            f"Non-arxiv paper leaked into source_type=arxiv filter: {p}"
        )


# --- State transitions: trash and restore ---


async def test_trash_paper_state_transition(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/trash: paper state transitions to 'trash' and
    state_before_trash is recorded (CASE expression atomically).

    Replaces test_trash_paper_sets_state_trash_and_records_state_before_trash
    (SQL: CASE expression + state_before_trash + state='trash' substrings).
    The contract test verifies the observable behavioral outcome: the DB row
    actually has state='trash' after the call.
    """
    # Seed a fresh paper so this test doesn't clobber the shared fixture paper
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'Trash Test Paper', ARRAY['Author'], 'https://inbox.test/trash', $2)
           RETURNING id""",
        "b109-trash-test-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, $2, 'inbox')",
        paper_id,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/trash")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    row = await contract_conn.fetchrow(
        "SELECT state, state_before_trash FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after trash"
    assert row["state"] == "trash", f"Expected state='trash', got {row['state']!r}"
    assert row["state_before_trash"] == "inbox", (
        f"Expected state_before_trash='inbox', got {row['state_before_trash']!r}"
    )


async def test_restore_paper_state_transition(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/restore: paper transitions from trash back to state_before_trash.

    Replaces test_restore_paper_returns_state_before_trash_to_state (SQL:
    COALESCE(state_before_trash,'inbox') substring match). The contract test verifies
    the DB row has state='reading' (the prior state) and state_before_trash=NULL.
    """
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'Restore Test Paper', ARRAY['Author'], 'https://inbox.test/restore', $2)
           RETURNING id""",
        "b109-restore-test-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)
           VALUES ($1, $2, 'trash', 'reading')""",
        paper_id,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/restore")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    row = await contract_conn.fetchrow(
        "SELECT state, state_before_trash FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after restore"
    assert row["state"] == "reading", (
        f"Expected state='reading' (restored from state_before_trash), got {row['state']!r}"
    )
    assert row["state_before_trash"] is None, (
        f"Expected state_before_trash=NULL after restore, got {row['state_before_trash']!r}"
    )


# --- PUT /api/papers/{id}/annotations partial update ---


async def test_annotations_partial_update_preserves_other_fields(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/annotations with only rating set: user_notes/flagged preserved.

    Replaces test_annotate_paper_partial_update_only_rating (SQL: COALESCE($4,
    paper_user_state.user_notes) + COALESCE($5, paper_user_state.flagged) substrings).
    The contract test verifies COALESCE semantics via real DB: pre-existing user_notes
    must survive a rating-only update.
    """
    paper_id = contract_two_users.paper_id_a
    # Set an existing user_notes value via annotations
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        setup_resp = await c.put(
            f"/api/papers/{paper_id}/annotations",
            json={"user_notes": "preserve-me", "flagged": True},
        )
    assert setup_resp.status_code == 200, f"Setup failed: {setup_resp.text[:300]}"

    # Partial update: only rating — user_notes/flagged must survive
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(
            f"/api/papers/{paper_id}/annotations",
            json={"rating": 3},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["rating"] == 3, f"Expected rating=3, got {body['rating']}"
    assert body["user_notes"] == "preserve-me", (
        f"user_notes should be preserved by partial update; got {body['user_notes']!r}"
    )
    assert body["flagged"] is True, (
        f"flagged should be preserved by partial update; got {body['flagged']}"
    )


# --- DELETE /api/papers/{id}/feedback cross-user scoping ---


async def test_delete_paper_feedback_removes_row_scoped_to_user(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """DELETE /api/papers/{id}/feedback: deletes caller's feedback row and NOT other user's.

    Replaces:
    - test_delete_paper_feedback_returns_204_for_existing_row (SQL: paper_id=$1, user_id=$2, source=$3)
    - test_delete_paper_feedback_scoped_to_exact_user (SQL: IS NOT DISTINCT FROM not in sql, user_id=$2)
    - test_delete_paper_feedback_different_user_id_not_deleted (SQL: positional[1]==99 param check)

    The contract test verifies behavioral scoping: user A's feedback row is deleted;
    user B's row for the same paper is untouched.
    """
    paper_id = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    # Seed feedback rows for both users
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'positive', 'feed_thumbs')
           ON CONFLICT (paper_id, user_id, source) DO UPDATE SET signal='positive'""",
        paper_id,
        user_a_id,
    )
    # Seed user B feedback on the SAME paper (user B can see shared paper via contract setup)
    # Use a paper that user B discovers; simpler: just insert directly
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'negative', 'feed_thumbs')
           ON CONFLICT (paper_id, user_id, source) DO UPDATE SET signal='negative'""",
        paper_id,
        user_b_id,
    )

    # User A deletes their feedback
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/papers/{paper_id}/feedback", params={"source": "feed_thumbs"})

    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text[:300]}"

    # User A's row must be gone
    a_row = await contract_conn.fetchrow(
        "SELECT 1 FROM recommendation_feedback WHERE paper_id=$1 AND user_id=$2 AND source='feed_thumbs'",
        paper_id,
        user_a_id,
    )
    assert a_row is None, "User A's feedback row should be deleted"

    # User B's row must be intact (exact user_id scoping, not IS NOT DISTINCT FROM)
    b_row = await contract_conn.fetchrow(
        "SELECT 1 FROM recommendation_feedback WHERE paper_id=$1 AND user_id=$2 AND source='feed_thumbs'",
        paper_id,
        user_b_id,
    )
    assert b_row is not None, "User B's feedback row must NOT be deleted by user A's DELETE call"


# ---------------------------------------------------------------------------
# Owner-path tests for uncovered rows
# ---------------------------------------------------------------------------


# --- A43: GET /api/papers/feed — user sees own library (feed endpoint) ---


async def test_a43_get_papers_feed_owner_sees_own_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A43: GET /api/papers/feed returns user A's library papers.
    Verified: services/paper_ingestion/paper_ingestion/routers/feed.py:34 at HEAD ba1f8146.
    Survivor-of: mock-unit feed-scoping tests in test_papers_router.py.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "papers" in body, f"FeedResponse must have 'papers' key; got: {list(body)}"
    assert "total" in body, f"FeedResponse must have 'total' key; got: {list(body)}"
    # User A's seeded paper (to_read state) must appear in the default feed
    ids = [p["id"] for p in body["papers"]]
    assert contract_two_users.paper_id_a in ids, (
        f"paper_id_a={contract_two_users.paper_id_a} not in user A's feed: {ids}"
    )


# --- A67: GET /api/papers/{paper_id} — owner gets 200 + full detail ---


async def test_a67_get_paper_detail_owner_gets_200(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A67: GET /api/papers/{paper_id} returns full PaperDetailResponse for owner.
    Verified: services/paper_ingestion/paper_ingestion/routers/papers_detail.py:59.

    The procrastinate_jobs subquery binds str(paper_id)/str(user_id) at the Python
    level, so the $1::text/$2::text casts in SQL are no-ops — no integer/text type
    conflict is possible on the connection pool. The route returns 200 deterministically.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/papers/{paper_id}")

    assert resp.status_code == 200, f"Owner expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "paper" in body, f"PaperDetailResponse must have 'paper' key; got: {list(body)}"
    assert body["paper"]["id"] == paper_id, (
        f"Returned paper id {body['paper']['id']} != expected {paper_id}"
    )


# --- A68: POST /api/papers/batch-save — owner can save list of papers ---


async def test_a68_batch_save_inserts_into_user_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A68: POST /api/papers/batch-save inserts papers into user_library with correct user_id.
    Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:356 at HEAD ba1f8146.
    Survivor-of: test_papers_router.py mock-unit batch-save tests.
    """
    payload = [
        {
            "external_id": "a68-contract-test-ext-001",
            "source_type": "arxiv",
            "title": "A68 Contract Batch Save Test",
            "authors": ["Test Author"],
            "url": "https://a68.contract.test/001",
        }
    ]
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/papers/batch-save", json=payload)

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, list) and len(body) == 1, (
        f"Expected list of 1 PaperResponse; got: {body!r}"
    )
    saved_paper_id = body[0]["id"]

    # Verify the paper is now in user A's library
    row = await contract_conn.fetchrow(
        "SELECT 1 FROM user_library WHERE paper_id=$1 AND user_id=$2",
        saved_paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, (
        f"Paper {saved_paper_id} not found in user A's user_library after batch-save"
    )


# --- A69: POST /api/papers/{paper_id}/feedback — owner can post feedback on system-discovered paper ---


async def test_a69_submit_feedback_owner_creates_row(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A69: POST /api/papers/{paper_id}/feedback creates feedback row scoped to user_id.
    Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:393 at HEAD ba1f8146.

    The seeded fixture paper has discovery_origin='user_initiated' which is excluded from
    recommendation training. This test seeds a fresh paper with discovery_origin='pulse'
    so the endpoint accepts the feedback.
    """
    # Seed a system-discovered paper for user A (pulse origin bypasses the rejection gate)
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers
               (external_id, source_type, title, authors, url, discovered_by, discovery_origin)
           VALUES ($1, 'arxiv', 'A69 Feedback Contract Paper', ARRAY['Author'],
                   'https://a69.contract.test/fb', $2, 'pulse')
           RETURNING id""",
        "a69-feedback-contract-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/papers/{paper_id}/feedback",
            json={"signal": "positive", "source": "feed_thumbs"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["signal"] == "positive", f"Expected signal='positive'; got {body['signal']!r}"
    assert body["paper_id"] == paper_id, f"Expected paper_id={paper_id}; got {body['paper_id']}"

    # Verify the feedback row is in the DB
    row = await contract_conn.fetchrow(
        "SELECT signal FROM recommendation_feedback WHERE paper_id=$1 AND user_id=$2 AND source='feed_thumbs'",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "Feedback row must exist after POST /api/papers/{id}/feedback"
    assert row["signal"] == "positive", f"Expected signal='positive' in DB; got {row['signal']!r}"


# --- A71: GET /api/papers/feed/counts — per-state counts scoped to user library ---


async def test_a71_get_feed_counts_reflects_user_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A71: GET /api/papers/feed/counts returns inbox/reading_list counts for user.
    Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:484 at HEAD ba1f8146.
    Survivor-of: test_feed_facet_counts.py mock-unit count tests.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed/counts")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    for field in ("inbox", "reading_list", "reading", "done", "starred", "trash"):
        assert field in body, f"FeedCountsResponse missing field {field!r}: {list(body)}"
    # The seeded fixture paper (state='to_read') must be counted in reading_list
    assert body["reading_list"] >= 1, (
        f"reading_list count should be ≥1 (seeded paper is to_read); got {body['reading_list']}"
    )


# --- A74: PUT /api/papers/{paper_id}/skip — owner can skip inbox paper ---


async def test_a74_skip_paper_transitions_state_to_done(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A74: PUT /api/papers/{paper_id}/skip sets paper_user_state to 'done'.
    Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:563 at HEAD ba1f8146.
    Survivor-of: test_papers_router.py skip mock-unit tests.

    skip_paper requires state='inbox'; seed a fresh paper in inbox state.
    """
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'A74 Skip Contract Paper', ARRAY['Author'],
                   'https://a74.contract.test/skip', $2)
           RETURNING id""",
        "a74-skip-contract-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    # No paper_user_state row → COALESCE default = 'inbox'; skip requires inbox state

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/skip")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after skip"
    assert row["state"] == "done", f"Expected state='done' after skip; got {row['state']!r}"


# --- A81: PUT /api/papers/{paper_id}/trash_and_reject — atomically trashes + records negative feedback ---


async def test_a81_trash_and_reject_trashes_and_inserts_feedback(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A81: PUT /api/papers/{paper_id}/trash_and_reject trashes paper and inserts
    negative feedback row atomically (source='dismiss_combined').
    Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:762 at HEAD ba1f8146.
    Survivor-of: test_papers_lifecycle.py mock-unit trash_and_reject test.
    """
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'A81 TrashReject Contract Paper', ARRAY['Author'],
                   'https://a81.contract.test/tr', $2)
           RETURNING id""",
        "a81-trash-reject-contract-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, $2, 'inbox')",
        paper_id,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/trash_and_reject")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"

    # Verify state='trash' in paper_user_state
    state_row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert state_row is not None, "paper_user_state row must exist after trash_and_reject"
    assert state_row["state"] == "trash", (
        f"Expected state='trash' after trash_and_reject; got {state_row['state']!r}"
    )

    # Verify negative feedback row inserted with source='dismiss_combined'
    fb_row = await contract_conn.fetchrow(
        """SELECT signal FROM recommendation_feedback
           WHERE paper_id=$1 AND user_id=$2 AND source='dismiss_combined'""",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert fb_row is not None, "Negative feedback row must be inserted by trash_and_reject"
    assert fb_row["signal"] == "negative", f"Expected signal='negative'; got {fb_row['signal']!r}"


# --- A83: DELETE /api/papers/{paper_id} — hard delete trashed paper ---


async def test_a83_hard_delete_removes_only_the_callers_membership(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """A session-scoped delete preserves the canonical paper row.
    Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:831 at HEAD ba1f8146.
    Survivor-of: test_papers_lifecycle.py mock-unit hard_delete tests.

    hard_delete requires state='trash'. Seeds a fresh paper in trash state.
    Qdrant cleanup is best-effort and not asserted (Qdrant is an exempt boundary).
    """
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'A83 HardDelete Contract Paper', ARRAY['Author'],
                   'https://a83.contract.test/del', $2)
           RETURNING id""",
        "a83-hard-delete-contract-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)"
        " VALUES ($1, $2, 'trash', 'inbox')",
        paper_id,
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/papers/{paper_id}")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("deleted") == paper_id, f"Expected {{'deleted': {paper_id}}}; got {body}"

    preserved = await contract_conn.fetchval("SELECT id FROM papers WHERE id=$1", paper_id)
    assert preserved == paper_id
    assert (
        await contract_conn.fetchval(
            "SELECT 1 FROM user_library WHERE paper_id=$1 AND user_id=$2",
            paper_id,
            contract_two_users.user_a_id,
        )
        is None
    )


# --- A84: POST /api/papers/bulk — bulk state action scoped to current user ---


async def test_a84_bulk_action_transitions_state_for_owner(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A84: POST /api/papers/bulk applies bulk state change to owner's papers.
    Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:891 at HEAD ba1f8146.
    Survivor-of: test_papers_lifecycle.py mock-unit bulk_action tests.

    Seeds a fresh inbox paper; bulk action 'save' should transition it to 'to_read'.
    """
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'A84 Bulk Contract Paper', ARRAY['Author'],
                   'https://a84.contract.test/bulk', $2)
           RETURNING id""",
        "a84-bulk-contract-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    # No paper_user_state row → COALESCE default = 'inbox'; bulk 'save' requires inbox-compatible state

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/papers/bulk",
            json={"paper_ids": [paper_id], "action": "save"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert paper_id in body.get("succeeded", []), (
        f"paper_id={paper_id} not in succeeded list; body={body}"
    )
    assert body.get("failed", []) == [], f"Expected no failures; got: {body.get('failed')}"

    # Verify state was actually transitioned in DB
    row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after bulk save"
    assert row["state"] == "to_read", (
        f"Expected state='to_read' after bulk save; got {row['state']!r}"
    )


# --- CRIT-XT-1: multi-tenant hard-delete must preserve a still-visible paper row ---


async def _seed_caller_private_paper_rows(
    conn, paper_id: int, user_id: int, other_id: int
) -> tuple[int, int]:
    suffix = f"{paper_id}-{user_id}"
    entity_id = await conn.fetchval(
        """INSERT INTO entities (name, canonical_name, entity_type)
           VALUES ($1, $1, 'concept') RETURNING id""",
        f"delete-private-entity-{suffix}",
    )
    template_id = await conn.fetchval(
        "INSERT INTO extraction_templates (name) VALUES ($1) RETURNING id",
        f"delete-private-template-{suffix}",
    )
    project_id = await conn.fetchval(
        "INSERT INTO projects (name, user_id) VALUES ($1, $2) RETURNING id",
        f"delete-private-project-{suffix}",
        user_id,
    )
    task_id = await conn.fetchval(
        "INSERT INTO tasks (project_id, title, user_id) VALUES ($1, $2, $3) RETURNING id",
        project_id,
        f"delete-private-task-{suffix}",
        user_id,
    )
    pulse_deck_id = await conn.fetchval(
        """INSERT INTO pulse_decks (deck_date, user_id)
           VALUES (CURRENT_DATE, $1)
           ON CONFLICT (deck_date, user_id) DO UPDATE SET user_id=EXCLUDED.user_id
           RETURNING id""",
        user_id,
    )

    await conn.execute(
        "INSERT INTO author_alert_log (paper_id, user_id) VALUES ($1, $2)", paper_id, user_id
    )
    await conn.execute(
        """INSERT INTO cards (paper_id, card_type, front, back, user_id)
           VALUES ($1, 'concept', $3, 'back', $2)""",
        paper_id,
        user_id,
        f"delete-private-card-{suffix}",
    )
    await conn.execute(
        """INSERT INTO paper_contradictions
             (paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
              explanation, confidence, user_id)
           VALUES ($1, $3, 'a', 'b', $4, $5, 'explanation', 0.9, $2)""",
        paper_id,
        user_id,
        other_id,
        f"quote-a-{suffix}",
        f"quote-b-{suffix}",
    )
    await conn.execute(
        "INSERT INTO paper_entities (paper_id, entity_id, user_id) VALUES ($1, $3, $2)",
        paper_id,
        user_id,
        entity_id,
    )
    await conn.execute(
        """INSERT INTO paper_extractions (paper_id, template_id, user_id)
           VALUES ($1, $3, $2)""",
        paper_id,
        user_id,
        template_id,
    )
    await conn.execute(
        """INSERT INTO paper_highlights (paper_id, user_id, page, rect, note)
           VALUES ($1, $2, 1, '{}'::jsonb, $3)""",
        paper_id,
        user_id,
        f"delete-private-highlight-{suffix}",
    )
    await conn.execute(
        "INSERT INTO paper_notes (paper_id, user_id, user_note) VALUES ($1, $2, $3)",
        paper_id,
        user_id,
        f"delete-private-note-{suffix}",
    )
    await conn.execute(
        "INSERT INTO paper_recommendations (paper_id, score, user_id) VALUES ($1, 0.8, $2)",
        paper_id,
        user_id,
    )
    await conn.execute(
        """INSERT INTO paper_summaries
             (paper_id, summary_brief, summary_detailed, user_id)
           VALUES ($1, $3, $3, $2)""",
        paper_id,
        user_id,
        f"delete-private-summary-{suffix}",
    )
    await conn.execute(
        """INSERT INTO paper_user_zotero_links (paper_id, user_id, zotero_item_key)
           VALUES ($1, $2, $3)""",
        paper_id,
        user_id,
        f"zotero-{suffix}",
    )
    await conn.execute(
        """INSERT INTO pulse_cards (deck_id, paper_id, rank, score, user_id)
           VALUES ($3, $1, 1, 0.8, $2)""",
        paper_id,
        user_id,
        pulse_deck_id,
    )
    await conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'positive', 'paper_detail_thumbs')""",
        paper_id,
        user_id,
    )
    await conn.execute(
        "INSERT INTO task_paper_links (task_id, paper_id) VALUES ($2, $1)", paper_id, task_id
    )
    await conn.execute(
        "INSERT INTO project_papers (project_id, paper_id) VALUES ($2, $1)", paper_id, project_id
    )
    await conn.execute(
        """UPDATE entities AS entity
           SET paper_count = (SELECT count(*) FROM paper_entities WHERE entity_id = entity.id)
           WHERE entity.id = $1""",
        entity_id,
    )
    await conn.execute(
        """UPDATE pulse_decks AS deck
           SET card_count = (SELECT count(*) FROM pulse_cards WHERE deck_id = deck.id)
           WHERE deck.id = $1""",
        pulse_deck_id,
    )
    return entity_id, pulse_deck_id


async def _caller_private_paper_counts(conn, paper_id: int, user_id: int) -> dict[str, int]:
    queries = {
        "author_alert_log": "SELECT count(*) FROM author_alert_log WHERE paper_id=$1 AND user_id=$2",
        "cards": "SELECT count(*) FROM cards WHERE paper_id=$1 AND user_id=$2",
        "paper_contradictions": """SELECT count(*) FROM paper_contradictions
            WHERE (paper_a_id=$1 OR paper_b_id=$1) AND user_id=$2""",
        "paper_entities": "SELECT count(*) FROM paper_entities WHERE paper_id=$1 AND user_id=$2",
        "paper_extractions": "SELECT count(*) FROM paper_extractions WHERE paper_id=$1 AND user_id=$2",
        "paper_highlights": "SELECT count(*) FROM paper_highlights WHERE paper_id=$1 AND user_id=$2",
        "paper_notes": "SELECT count(*) FROM paper_notes WHERE paper_id=$1 AND user_id=$2",
        "paper_recommendations": "SELECT count(*) FROM paper_recommendations WHERE paper_id=$1 AND user_id=$2",
        "paper_summaries": "SELECT count(*) FROM paper_summaries WHERE paper_id=$1 AND user_id=$2",
        "paper_user_zotero_links": "SELECT count(*) FROM paper_user_zotero_links WHERE paper_id=$1 AND user_id=$2",
        "pulse_cards": "SELECT count(*) FROM pulse_cards WHERE paper_id=$1 AND user_id=$2",
        "recommendation_feedback": "SELECT count(*) FROM recommendation_feedback WHERE paper_id=$1 AND user_id=$2",
        "task_paper_links": """SELECT count(*) FROM task_paper_links link
            JOIN tasks owner ON owner.id=link.task_id
            WHERE link.paper_id=$1 AND owner.user_id=$2""",
        "project_papers": """SELECT count(*) FROM project_papers link
            JOIN projects owner ON owner.id=link.project_id
            WHERE link.paper_id=$1 AND owner.user_id=$2""",
        "paper_user_state": "SELECT count(*) FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        "user_library": "SELECT count(*) FROM user_library WHERE paper_id=$1 AND user_id=$2",
    }
    return {name: await conn.fetchval(sql, paper_id, user_id) for name, sql in queries.items()}


async def test_hard_delete_shared_paper_removes_only_callers_private_rows(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """User A hard-deletes a shared paper without deleting B's data or shared artifacts.

    Every paper-linked row that is privately owned by A is removed in the same
    transaction. B's corresponding rows and the canonical chunk remain.
    """
    from paper_ingestion import papers_service

    vector_calls: list[int] = []

    async def _spy_delete_vectors(pid: int) -> None:
        vector_calls.append(pid)

    monkeypatch.setattr(papers_service, "delete_paper_vectors", _spy_delete_vectors)

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('xt1-shared', 'arxiv', 'XT1 Shared Paper', ARRAY['Au'],
                   'https://xt1.t/shared', $1)
           RETURNING id""",
        user_a_id,
    )
    for uid in (user_a_id, user_b_id):
        await contract_conn.execute(
            "INSERT INTO user_library (user_id, paper_id, added_via)"
            " VALUES ($1, $2, 'manual_save')",
            uid,
            paper_id,
        )
    # A trashed it; B is actively reading it.
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)"
        " VALUES ($1, $2, 'trash', 'inbox')",
        paper_id,
        user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, $2, 'reading')",
        paper_id,
        user_b_id,
    )
    other_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ($1, 'arxiv', 'XT1 Comparison Paper', ARRAY['Au'], $2)
           RETURNING id""",
        f"xt1-comparison-{paper_id}",
        f"https://xt1.t/comparison/{paper_id}",
    )
    await contract_conn.execute(
        """INSERT INTO paper_chunks (paper_id, chunk_index, content)
           VALUES ($1, 0, 'shared canonical chunk')""",
        paper_id,
    )
    entity_a_id, pulse_deck_a_id = await _seed_caller_private_paper_rows(
        contract_conn, paper_id, user_a_id, other_id
    )
    entity_b_id, pulse_deck_b_id = await _seed_caller_private_paper_rows(
        contract_conn, paper_id, user_b_id, other_id
    )
    assert set(
        (await _caller_private_paper_counts(contract_conn, paper_id, user_a_id)).values()
    ) == {1}
    assert set(
        (await _caller_private_paper_counts(contract_conn, paper_id, user_b_id)).values()
    ) == {1}
    assert await contract_conn.fetchval(
        "SELECT paper_count FROM entities WHERE id=$1", entity_a_id
    ) == await contract_conn.fetchval(
        "SELECT count(*) FROM paper_entities WHERE entity_id=$1", entity_a_id
    )
    assert await contract_conn.fetchval(
        "SELECT paper_count FROM entities WHERE id=$1", entity_b_id
    ) == await contract_conn.fetchval(
        "SELECT count(*) FROM paper_entities WHERE entity_id=$1", entity_b_id
    )
    for deck_id in (pulse_deck_a_id, pulse_deck_b_id):
        assert await contract_conn.fetchval(
            "SELECT card_count FROM pulse_decks WHERE id=$1", deck_id
        ) == await contract_conn.fetchval(
            "SELECT count(*) FROM pulse_cards WHERE deck_id=$1", deck_id
        )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/papers/{paper_id}")

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:300]}"

    # Shared row + B's data survive.
    shared_paper_row = await contract_conn.fetchval("SELECT id FROM papers WHERE id=$1", paper_id)
    assert shared_paper_row == paper_id
    assert (
        await contract_conn.fetchval(
            "SELECT 1 FROM user_library WHERE paper_id=$1 AND user_id=$2", paper_id, user_b_id
        )
        == 1
    )
    assert (
        await contract_conn.fetchval(
            "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
            paper_id,
            user_b_id,
        )
        == "reading"
    )
    # Every caller-private row/link is gone, while B's complete private surface
    # and the canonical chunk remain.
    assert set(
        (await _caller_private_paper_counts(contract_conn, paper_id, user_a_id)).values()
    ) == {0}
    assert set(
        (await _caller_private_paper_counts(contract_conn, paper_id, user_b_id)).values()
    ) == {1}
    assert (
        await contract_conn.fetchval(
            "SELECT content FROM paper_chunks WHERE paper_id=$1 AND chunk_index=0", paper_id
        )
        == "shared canonical chunk"
    )
    # Shared vectors NOT touched (row survives).
    assert vector_calls == [], (
        f"delete_paper_vectors must not run when row survives: {vector_calls}"
    )


async def test_hard_delete_last_holder_preserves_shared_row_and_vectors(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """A last library holder still cannot delete shared canonical data."""
    from paper_ingestion import papers_service

    vector_calls: list[int] = []

    async def _spy_delete_vectors(pid: int) -> None:
        vector_calls.append(pid)

    monkeypatch.setattr(papers_service, "delete_paper_vectors", _spy_delete_vectors)

    user_a_id = contract_two_users.user_a_id
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('xt1-solo', 'arxiv', 'XT1 Solo Paper', ARRAY['Au'],
                   'https://xt1.t/solo', $1)
           RETURNING id""",
        user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_a_id,
        paper_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)"
        " VALUES ($1, $2, 'trash', 'inbox')",
        paper_id,
        user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/papers/{paper_id}")

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:300]}"
    preserved = await contract_conn.fetchval("SELECT id FROM papers WHERE id=$1", paper_id)
    assert preserved == paper_id
    assert vector_calls == []


async def test_hard_delete_sole_holder_preserves_shared_row_regardless_of_discoverer(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """A session-scoped delete never removes the canonical paper row.

    A is the only library holder, while B is the recorded discoverer. A's
    private rows are removed, but shared SQL and Qdrant data remain available
    independently of membership count or discovery attribution.
    """
    from paper_ingestion import papers_service

    vector_calls: list[int] = []

    async def _spy_delete_vectors(pid: int) -> None:
        vector_calls.append(pid)

    monkeypatch.setattr(papers_service, "delete_paper_vectors", _spy_delete_vectors)

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id
    # Paper discovered by B, but only A holds it in their library (B does NOT).
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('xt1-guard', 'arxiv', 'XT1 Guard Paper', ARRAY['Au'],
                   'https://xt1.t/guard', $1)
           RETURNING id""",
        user_b_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_a_id,
        paper_id,
    )
    # A trashed it (hard-delete requires state='trash').
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)"
        " VALUES ($1, $2, 'trash', 'inbox')",
        paper_id,
        user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/papers/{paper_id}")

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:300]}"

    # The shared papers row is preserved even after the last membership is gone.
    surviving_paper_row = await contract_conn.fetchval(
        "SELECT id FROM papers WHERE id=$1", paper_id
    )
    assert surviving_paper_row == paper_id, (
        f"shared papers row {paper_id} must survive a session-scoped delete"
    )
    # (a) A's user_library + paper_user_state rows are gone.
    a_library_row = await contract_conn.fetchval(
        "SELECT 1 FROM user_library WHERE paper_id=$1 AND user_id=$2", paper_id, user_a_id
    )
    assert a_library_row is None, "A's user_library membership must be removed"
    a_state_row = await contract_conn.fetchval(
        "SELECT 1 FROM paper_user_state WHERE paper_id=$1 AND user_id=$2", paper_id, user_a_id
    )
    assert a_state_row is None, "A's paper_user_state row must be removed"
    # Shared vectors are not touched because the row was not physically deleted.
    assert vector_calls == [], (
        f"delete_paper_vectors must not run for a session-scoped delete: {vector_calls}"
    )


# --- A90: POST /api/papers/batch-process — queues job for user's unprocessed papers ---


async def test_a90_batch_process_returns_job_id(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A90: POST /api/papers/batch-process returns {queued, job_id} dict.
    Verified: services/paper_ingestion/paper_ingestion/routers/pdf.py:384 at HEAD ba1f8146.
    Survivor-of: test_pdf_router_direct.py mock-unit batch_process tests.

    This endpoint queries for papers with pdf_downloaded=True in the user's library;
    in the contract test environment there are no such papers, so queued=0 and
    job_id may be None. The behavioral contract is: endpoint returns 200 with
    the expected response shape and does NOT raise on an empty eligible set.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/papers/batch-process", params={"limit": 5})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "queued" in body, f"batch-process response must have 'queued' key; got: {list(body)}"
    assert "total_unprocessed" in body, (
        f"batch-process response must have 'total_unprocessed' key; got: {list(body)}"
    )


# --- A92: POST /api/papers/recompute-priorities — updates priority scores ---


async def test_a92_recompute_all_priorities_returns_updated_count(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A92: POST /api/papers/recompute-priorities returns {updated: N}.
    Verified: services/paper_ingestion/paper_ingestion/routers/priority.py:77 at HEAD ba1f8146.
    Survivor-of: test_priority.py mock-unit recompute tests.

    The endpoint recomputes priorities across ALL papers (not scoped by user) and
    returns the count of updated rows. With at least the seeded paper in the DB,
    updated >= 1.
    """
    from jarvis_common.auth import require_admin

    async def _allow_admin():
        return None

    _pi_app_with_pool.dependency_overrides[require_admin] = _allow_admin
    try:
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/papers/recompute-priorities")
    finally:
        _pi_app_with_pool.dependency_overrides.pop(require_admin, None)

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "updated" in body, (
        f"recompute-priorities response must have 'updated' key; got: {list(body)}"
    )
    assert isinstance(body["updated"], int), (
        f"'updated' must be an integer; got {type(body['updated'])}"
    )
    assert body["updated"] >= 1, (
        f"Expected updated >= 1 (at least 1 paper seeded by fixture); got {body['updated']}"
    )


# ---------------------------------------------------------------------------
# RD-DA-003: BM25 user_id scope leak — hybrid search cross-user isolation
# ---------------------------------------------------------------------------


async def test_list_papers_bm25_no_cross_user_leak(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """RD-DA-003: POST /api/papers/search-hybrid — BM25 leg must not expose another user's paper.

    Seeds a paper with a unique sentinel title owned ONLY by user A.  User B
    searches for the sentinel term.  Without the user_library JOIN fix the BM25
    leg would return user A's paper; with the fix user B must get an empty list.

    The test mocks the embedder so it is Qdrant-free (no GPU required); only
    the SQL path is under test.

    Verified: services/paper_ingestion/paper_ingestion/ingestion/search.py:386
    — hybrid_search BM25 SQL now JOIN user_library ul ON ul.user_id = $3.
    """
    from unittest.mock import AsyncMock, patch

    from paper_ingestion.ingestion.embedder import Embedder

    sentinel = "xq7bm25leaktest2026unique"

    # Seed a paper owned ONLY by user A with the sentinel term in the title.
    leak_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers
               (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', $2, ARRAY['Author'], 'https://bm25leak.test/1', $3)
           RETURNING id""",
        "bm25-leak-test-ext-rdd-da-003",
        f"Hybrid Search {sentinel} Paper",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        leak_paper_id,
    )
    # Update the search_vector so BM25 can match it
    await contract_conn.execute(
        "UPDATE papers SET search_vector = to_tsvector('english', title) WHERE id = $1",
        leak_paper_id,
    )

    # Build a minimal app-level embedder mock: Qdrant available but returns no chunks.
    # This isolates the BM25 leg (semantic leg returns empty).
    mock_embedder = AsyncMock(spec=Embedder)
    mock_embedder.qdrant = object()  # truthy — passes the qdrant is None guard
    mock_embedder.search_chunks_global = AsyncMock(return_value=[])
    # hybrid_search must run real code, not be fully mocked; use the real method
    # bound to the mock so self.search_chunks_global is patched but BM25 SQL runs.
    from paper_ingestion.ingestion.search import EmbeddingSearchMixin

    mock_embedder.hybrid_search = lambda *a, **kw: EmbeddingSearchMixin.hybrid_search(
        mock_embedder, *a, **kw
    )

    from paper_ingestion.routers.search import get_embedder

    with patch.dict(
        _pi_app_with_pool.dependency_overrides,
        {get_embedder: lambda: mock_embedder},
    ):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
            resp = await c.post(
                "/api/papers/search-hybrid",
                json={"query": sentinel, "max_results": 10},
            )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    ids = [p["id"] for p in resp.json()]
    assert leak_paper_id not in ids, (
        f"RD-DA-003 FAIL: user B's hybrid search returned user A's paper "
        f"(paper_id={leak_paper_id}) — BM25 scope leak. ids={ids}"
    )


# ---------------------------------------------------------------------------
# E1.PI extensions — bulk partial-failure isolation + idempotent annotation
#
# Verified: papers.py:889-946 (bulk_action_papers — per-paper SAVEPOINT isolation)
# Verified: papers.py:807 (annotate_paper — assert_paper_ownership + RETURNING)
# ---------------------------------------------------------------------------


async def test_e1_bulk_action_partial_failure_isolation(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """POST /api/papers/bulk: user A bulk-deletes 3 papers; 1 belongs to user B → B's untouched.

    This is the per-paper SAVEPOINT isolation guarantee: a 403/404 on one paper
    must not roll back the outer transaction, and must not affect an unrelated paper.
    Verified: papers.py:889-946 bulk_action_papers (SAVEPOINT per iteration).
    Survivor-of: test_papers_lifecycle.py bulk IDOR mock-unit tests.
    """
    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    # Seed two papers for user A
    pid_a1 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('e1-bulk-a1', 'arxiv', 'Bulk Paper A1', ARRAY['Au'], 'https://e1.t/a1', $1)
           RETURNING id""",
        user_a_id,
    )
    pid_a2 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('e1-bulk-a2', 'arxiv', 'Bulk Paper A2', ARRAY['Au'], 'https://e1.t/a2', $1)
           RETURNING id""",
        user_a_id,
    )
    for pid in (pid_a1, pid_a2):
        await contract_conn.execute(
            "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
            user_a_id,
            pid,
        )

    # Seed a fresh paper owned by user B — user A must not be able to act on it
    pid_b_own = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('e1-bulk-b-own', 'arxiv', 'B Own Paper', ARRAY['Au'], 'https://e1.t/b', $1)
           RETURNING id""",
        user_b_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_b_id,
        pid_b_own,
    )

    # User A bulk-saves [pid_a1, pid_b_own (owned by B), pid_a2]
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/papers/bulk",
            json={"paper_ids": [pid_a1, pid_b_own, pid_a2], "action": "save"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    succeeded = body.get("succeeded", [])
    failed = body.get("failed", [])

    # A's own papers should succeed
    assert pid_a1 in succeeded, f"pid_a1 should succeed; succeeded={succeeded}"
    assert pid_a2 in succeeded, f"pid_a2 should succeed; succeeded={succeeded}"
    # B's paper should fail (not_found or forbidden) and NOT appear in succeeded
    assert pid_b_own not in succeeded, (
        f"User B's paper must not be in succeeded — IDOR isolation failure: {succeeded}"
    )
    failed_ids = [f["paper_id"] for f in failed]
    assert pid_b_own in failed_ids, (
        f"User B's paper must appear in failed list; failed_ids={failed_ids}"
    )

    # B's paper_user_state must remain untouched (no state row created for A's action)
    b_row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        pid_b_own,
        user_a_id,
    )
    assert b_row is None, (
        "User A's bulk action must not create a paper_user_state row for user B's paper"
    )


async def test_e1_annotations_idempotent_double_put(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/annotations twice yields the last write's state (idempotent UPSERT).

    Verified: papers.py:807 (annotate_paper — ON CONFLICT DO UPDATE path via assert_paper_ownership).
    Survivor-of: test_papers_lifecycle.py annotations idempotency tests.
    """
    paper_id = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp1 = await c.put(
            f"/api/papers/{paper_id}/annotations",
            json={"rating": 3, "user_notes": "first-write"},
        )
    assert resp1.status_code == 200, f"First PUT failed: {resp1.text[:200]}"

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp2 = await c.put(
            f"/api/papers/{paper_id}/annotations",
            json={"rating": 5, "user_notes": "second-write"},
        )
    assert resp2.status_code == 200, f"Second PUT failed: {resp2.text[:200]}"
    body = resp2.json()
    assert body["rating"] == 5, f"Expected last-write rating=5; got {body['rating']!r}"
    assert body["user_notes"] == "second-write"

    # Verify exactly one row in DB (idempotent UPSERT, not insert-per-call)
    count = await contract_conn.fetchval(
        "SELECT count(*) FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        user_a_id,
    )
    assert count == 1, (
        f"Exactly one paper_user_state row must exist after double PUT; got count={count}"
    )


# ---------------------------------------------------------------------------
# Cluster 6 — Papers router behaviors (star / restore-409 / detail flags / feedback)
# Survivor-of mock-units in test_papers_router.py:
#   test_star_paper_sets_starred_true_does_not_change_state (545) → C6-star
#   test_restore_paper_non_trash_returns_409 (600)               → C6-restore-409
#   test_get_paper_detail_processing_failed_true_when_last_job_failed (247) → C6-pf-flag
#   test_get_paper_detail_processing_failed_false_when_last_job_succeeded (281) → C6-pf-flag (sub-assertion)
#   test_get_paper_detail_sets_has_project_links_false_when_unlinked (304) → C6-hpl-flag
#   test_submit_feedback_rejects_user_initiated_papers (458)     → C6-feedback-reject
#   test_submit_feedback_accepts_system_discovered_origins (436, parametrize x3) → existing test_a69_submit_feedback_owner_creates_row (679)
#   test_submit_feedback_maps_foreign_key_violation_to_404 (405) → DEFERRED (the FK handler at submit_feedback:438-439 is defensive-only and unreachable in practice because the discovery_origin SELECT at lines 414-419 returns None first → 404 via that path; same response shape)
# ---------------------------------------------------------------------------


async def test_star_paper_sets_starred_true(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/star sets starred=TRUE in paper_user_state.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:624
    # (star_paper: upserts via CTE; zotero.push enqueue iff new-star + project-link
    # + zotero.auto_push_on_star=true; default config means no enqueue → no
    # task_registry carve-out needed for this test).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id

    # Reset starred=FALSE so star_paper exercises the off→on path
    await contract_conn.execute(
        "UPDATE paper_user_state SET starred = FALSE WHERE paper_id=$1 AND user_id=$2",
        paper_id_a,
        user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id_a}/star")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.json().get("status") == "ok"

    starred_now = await contract_conn.fetchval(
        "SELECT starred FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id_a,
        user_a_id,
    )
    assert starred_now is True, f"starred should be TRUE after PUT /star; got {starred_now!r}"


async def test_restore_non_trash_paper_returns_409(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/restore on a paper NOT in trash returns 409.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:738
    # (restore_paper calls _assert_paper_in_states(allowed=("trash",)) → 409 if
    # the paper's current state is not in the allowed set).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id

    # Make sure the paper is in a non-trash state ('to_read' from _seed_resources)
    cur_state = await contract_conn.fetchval(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id_a,
        user_a_id,
    )
    assert cur_state != "trash", (
        f"Test pre-condition: seeded paper must be in non-trash state; got {cur_state!r}"
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id_a}/restore")

    assert resp.status_code == 409, (
        f"Expected 409 restoring non-trash paper; got {resp.status_code}: {resp.text[:300]}"
    )


async def test_paper_detail_processing_failed_flag(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/papers/{id} reflects the latest paper.process job status as processing_failed=True.

    Seeds a procrastinate_jobs row with status='failed' for the paper; verifies
    the detail response surfaces processing_failed=True. Replaces the mock-unit
    that controlled the SQL response directly.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:231
    # (get_paper_detail computes processing_failed from the latest procrastinate
    # job with task_name in {paper.process, paper.analyze} for the paper).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id

    await contract_conn.execute(
        """
        INSERT INTO procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('paper_ingestion', 'paper.process', $1::jsonb, 'failed')
        """,
        {"paper_id": paper_id_a, "user_id": user_a_id, "job_id": "test-failed-job"},
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/papers/{paper_id_a}")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.json().get("processing_failed") is True, (
        f"Expected processing_failed=True after seeding failed paper.process job; "
        f"got: {resp.json().get('processing_failed')!r}"
    )


async def test_paper_detail_has_project_links_flag(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/papers/{id} reflects project_papers count as has_project_links.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:231
    # (get_paper_detail computes has_project_links from COUNT(*) > 0 of
    # project_papers rows for the paper).
    """
    paper_id_a = contract_two_users.paper_id_a
    project_id_a = contract_two_users.project_id_a

    # Initially no project_papers link — expect has_project_links=False
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_before = await c.get(f"/api/papers/{paper_id_a}")
    assert resp_before.status_code == 200, resp_before.text[:300]
    assert resp_before.json().get("has_project_links") in (False, None), (
        f"Pre-condition: no project links yet; got {resp_before.json().get('has_project_links')!r}"
    )

    # Add a project link
    await contract_conn.execute(
        "INSERT INTO project_papers (project_id, paper_id) VALUES ($1, $2)",
        project_id_a,
        paper_id_a,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_after = await c.get(f"/api/papers/{paper_id_a}")
    assert resp_after.status_code == 200, resp_after.text[:300]
    assert resp_after.json().get("has_project_links") is True, (
        f"Expected has_project_links=True after project_papers insert; got "
        f"{resp_after.json().get('has_project_links')!r}"
    )


async def test_paper_detail_has_project_links_not_leaked_across_users(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """has_project_links must be scoped to the caller's projects.

    User B links A's paper to B's *own* project. When user A views the paper
    detail, has_project_links must be False — B's link must not surface to A
    (it gates the "Send to Zotero" button, so a leak is a cross-tenant signal).

    Before the fix the COUNT(*) over project_papers was unscoped, so A saw True.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers_detail.py:89
    # (get_paper_detail's project-link count must JOIN projects scoped to user_id).
    """
    paper_id_a = contract_two_users.paper_id_a
    project_id_b = contract_two_users.project_id_b

    # User B links A's paper into B's project (B owns project_id_b).
    await contract_conn.execute(
        "INSERT INTO project_papers (project_id, paper_id) VALUES ($1, $2)",
        project_id_b,
        paper_id_a,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/papers/{paper_id_a}")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.json().get("has_project_links") in (False, None), (
        "User B's project link must NOT surface as has_project_links to user A; "
        f"got {resp.json().get('has_project_links')!r}"
    )


async def test_star_zotero_autopush_not_triggered_by_other_users_link(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, monkeypatch
):
    """Starring a paper only *another* user has linked
    must NOT trigger the Zotero auto-push.

    star_paper enqueues zotero.push iff (off->on star) AND (caller's project
    link count > 0) AND (auto_push_on_star). With the unscoped count, user A
    starring a paper that only user B linked would count B's link and wrongly
    enqueue a push. After scoping, A's link count is 0 → no enqueue.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers_lifecycle.py:177
    # (star_paper's project-link count must JOIN projects scoped to user_id).
    """
    from unittest.mock import AsyncMock, MagicMock

    import jarvis_common.task_registry as task_registry

    paper_id_a = contract_two_users.paper_id_a
    project_id_b = contract_two_users.project_id_b
    user_a_id = contract_two_users.user_a_id

    # Enable auto-push for user A.
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES ($1, 'zotero.auto_push_on_star', 'true'::jsonb)""",
        user_a_id,
    )
    # Ensure A's paper is currently unstarred so the call is an off->on transition.
    await contract_conn.execute(
        "UPDATE paper_user_state SET starred = FALSE WHERE paper_id = $1 AND user_id = $2",
        paper_id_a,
        user_a_id,
    )
    # Only user B links A's paper into B's own project.
    await contract_conn.execute(
        "INSERT INTO project_papers (project_id, paper_id) VALUES ($1, $2)",
        project_id_b,
        paper_id_a,
    )

    mock_task = MagicMock()
    mock_enqueue = AsyncMock()
    mock_task.defer_async = mock_enqueue
    monkeypatch.setitem(task_registry._TASK_MAP, "zotero.push", mock_task)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id_a}/star")

    assert resp.status_code == 200, resp.text[:300]
    mock_enqueue.assert_not_awaited()


async def test_feedback_rejects_user_initiated_paper(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/papers/{id}/feedback rejects user_initiated papers with 400.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:390
    # (submit_feedback returns 400 when paper.discovery_origin == 'user_initiated').
    """
    paper_id_a = contract_two_users.paper_id_a
    # Force discovery_origin to user_initiated (seed_resources sets it via discovered_by only)
    await contract_conn.execute(
        "UPDATE papers SET discovery_origin = 'user_initiated' WHERE id = $1",
        paper_id_a,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/papers/{paper_id_a}/feedback",
            json={"signal": "negative", "source": "feed_thumbs"},
        )

    assert resp.status_code == 400, (
        f"Expected 400 for user_initiated paper feedback; got {resp.status_code}: {resp.text[:300]}"
    )
    assert "user_initiated" in resp.text.lower(), (
        f"400 detail should mention user_initiated; got: {resp.text[:300]}"
    )


async def test_feedback_404_when_paper_deleted_or_missing(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/papers/{id}/feedback returns 404 when the paper does not exist.

    The defensive FK-violation handler at submit_feedback:438-439 is unreachable
    in practice because assert_paper_ownership at :412 returns 404 first when
    the paper row is missing. This test covers the observable HTTP boundary
    (404) regardless of which internal branch fires.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:390
    # (submit_feedback: assert_paper_ownership → 404 if paper missing).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/papers/9999999/feedback",
            json={"signal": "positive", "source": "feed_thumbs"},
        )

    assert resp.status_code in (403, 404), (
        f"Expected 403/404 for missing paper feedback; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Cluster 8 — Paper detail null user_state (one test; the 3 feed-filter tests
# of Cluster 8 live in test_feed_contract.py).
# Survivor-of: test_dashboard_api.py::test_paper_detail_user_state_null_when_absent
# ---------------------------------------------------------------------------


async def test_paper_detail_null_user_state(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """GET /api/papers/{id} returns user_state=null when no paper_user_state row exists.

    _seed_resources auto-creates a paper_user_state row for the seeded paper;
    so we seed a fresh paper without one and verify the null-state response shape.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:231
    # (get_paper_detail returns user_state=None when the paper_user_state row is missing).
    """
    user_a_id = contract_two_users.user_a_id
    fresh_paper_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('fresh-no-user-state', 'arxiv', 'Fresh paper', ARRAY['F'],
                'https://example.test/fresh', $1)
        RETURNING id
        """,
        user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_a_id,
        fresh_paper_id,
    )
    # Intentionally NOT inserting into paper_user_state.

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/papers/{fresh_paper_id}")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body.get("user_state") is None, (
        f"Expected user_state=null when no paper_user_state row; got {body.get('user_state')!r}"
    )


# ---------------------------------------------------------------------------
# Cluster 10 — Origin stamping (batch_save_papers → 'citation_batch')
# Survivor-of test_origin_stamping.py::test_batch_save_papers_stamps_citation_batch.
# Note: upload_pdf user_initiated + discovered_by is already covered by Cluster 4's
# test_p01_upload_pdf_stamps_user_initiated_and_adds_to_library.
# ---------------------------------------------------------------------------


async def test_batch_save_stamps_citation_batch_origin(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """POST /api/papers/batch-save stamps discovery_origin='citation_batch' on each paper.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:353
    # (batch_save_papers: `paper.discovery_origin = "citation_batch"` at line 378
    # overrides PaperCreate's default before upsert_paper).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/papers/batch-save",
            json=[
                {
                    "external_id": "batch-c10-1",
                    "source_type": "arxiv",
                    "title": "Batch paper one",
                    "authors": ["A. One"],
                    "abstract": "abstract",
                    "url": "https://example.test/b1",
                },
                {
                    "external_id": "batch-c10-2",
                    "source_type": "arxiv",
                    "title": "Batch paper two",
                    "authors": ["A. Two"],
                    "abstract": "abstract",
                    "url": "https://example.test/b2",
                },
            ],
        )

    assert resp.status_code == 200, resp.text[:300]

    rows = await contract_conn.fetch(
        "SELECT discovery_origin FROM papers WHERE external_id IN ('batch-c10-1', 'batch-c10-2')"
    )
    assert len(rows) == 2, f"Expected 2 inserted papers; got {len(rows)}"
    for row in rows:
        assert row["discovery_origin"] == "citation_batch", (
            f"Expected discovery_origin='citation_batch'; got {row['discovery_origin']!r}"
        )


# ---------------------------------------------------------------------------
# W1A.1 — Papers state-transition contract tests (8 tests)
#
# These 8 contracts cover the save / unsave / reading / done / unstar / IDOR /
# 404-on-missing / audit-trail endpoints in papers.py.
#
# Verified identifiers:
#   routers/papers.py:514 — save_paper PUT /{paper_id}/save → state='to_read'
#   routers/papers.py:536 — unsave_paper PUT /{paper_id}/unsave → state='inbox'
#   routers/papers.py:582 — reading_paper PUT /{paper_id}/reading → state='reading'
#   routers/papers.py:604 — done_paper PUT /{paper_id}/done → state='done'
#   routers/papers.py:698 — unstar_paper PUT /{paper_id}/unstar → starred=False
#   libs/jarvis_common/jarvis_common/db_helpers.py:234 — assert_paper_ownership → 403 non-owner
#
# CONTRACT-GAP (test_pwst_08): save_paper does NOT emit to system_events.
# Verified by grep 'system_events' in routers/papers.py → 0 matches.
# test_pwst_08 instead asserts the trigger-maintained updated_at column records
# the state-transition timestamp (observable audit trail via set_updated_at_paper_user_state).
# ---------------------------------------------------------------------------


async def test_pwst_01_save_transitions_user_state_to_saved(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/save: state of an inbox paper transitions to 'to_read'.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:514
    # (save_paper: _upsert_state_and_starred with state='to_read'; allowed from inbox).
    """
    # ARRANGE — seed a fresh paper in inbox state (no paper_user_state row)
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'PWST-01 Save Contract Paper', ARRAY['Author'],
                   'https://pwst01.contract.test/save', $2)
           RETURNING id""",
        "pwst01-save-contract-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    # No paper_user_state row → COALESCE default = 'inbox'; save requires inbox-compatible state.

    # ACT
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/save")

    # ASSERT
    assert resp.status_code in (200, 204), resp.text[:200]
    row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after PUT /save"
    assert row["state"] == "to_read", f"Expected state='to_read'; got {row['state']!r}"


async def test_pwst_02_unsave_clears_saved_state(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/unsave: state reverts from 'to_read' back to 'inbox'.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:536
    # (unsave_paper: allowed=('to_read',); _upsert_state_and_starred with state='inbox').
    """
    # ARRANGE — seeded paper_id_a is already in state='to_read' (from _seed_resources)
    paper_id = contract_two_users.paper_id_a

    # ACT
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/unsave")

    # ASSERT
    assert resp.status_code in (200, 204), resp.text[:200]
    row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after PUT /unsave"
    assert row["state"] == "inbox", f"Expected state='inbox' after unsave; got {row['state']!r}"


async def test_pwst_03_reading_transitions_to_reading(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/reading: state transitions to 'reading'.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:582
    # (reading_paper: allowed=('to_read','reading','done'); _upsert_state_and_starred
    # with state='reading').
    """
    # ARRANGE — seeded paper_id_a is in state='to_read', which is in the allowed set.
    paper_id = contract_two_users.paper_id_a

    # ACT
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/reading")

    # ASSERT
    assert resp.status_code in (200, 204), resp.text[:200]
    row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after PUT /reading"
    assert row["state"] == "reading", (
        f"Expected state='reading' after PUT /reading; got {row['state']!r}"
    )


async def test_pwst_04_done_transitions_to_done(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/done: state transitions to 'done' regardless of prior state.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:604
    # (done_paper: no _assert_paper_in_states restriction; _upsert_state_and_starred
    # with state='done').
    """
    # ARRANGE — seeded paper_id_a is in state='to_read'; done has no state restriction.
    paper_id = contract_two_users.paper_id_a

    # ACT
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/done")

    # ASSERT
    assert resp.status_code in (200, 204), resp.text[:200]
    row = await contract_conn.fetchrow(
        "SELECT state FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after PUT /done"
    assert row["state"] == "done", f"Expected state='done' after PUT /done; got {row['state']!r}"


async def test_pwst_05_unstar_clears_starred_flag(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/unstar: starred flag transitions to FALSE without changing state.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:698
    # (unstar_paper: _upsert_state_and_starred with starred=False; state unchanged).
    """
    # ARRANGE — seeded paper_id_a has starred=TRUE (from _seed_resources).
    paper_id = contract_two_users.paper_id_a
    prior_starred = await contract_conn.fetchval(
        "SELECT starred FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert prior_starred is True, (
        f"Pre-condition: seeded paper must be starred; got {prior_starred}"
    )

    # ACT
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/unstar")

    # ASSERT
    assert resp.status_code in (200, 204), resp.text[:200]
    row = await contract_conn.fetchrow(
        "SELECT state, starred FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after PUT /unstar"
    assert row["starred"] is False, (
        f"Expected starred=False after PUT /unstar; got {row['starred']!r}"
    )
    # State must not have been changed by unstar
    assert row["state"] == "to_read", (
        f"unstar must not change state; expected 'to_read', got {row['state']!r}"
    )


async def test_pwst_06_state_transitions_idor_rejected(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{paper_id_a}/save by user B returns 403 (IDOR rejection).

    # Verified: libs/jarvis_common/jarvis_common/db_helpers.py
    # (assert_paper_ownership requires persisted public scope or caller-library
    # membership; this private paper has neither for user B).
    """
    # ARRANGE — paper_id_a is private and absent from user B's library.
    paper_id = contract_two_users.paper_id_a

    # ACT — user B attempts to save user A's paper
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.put(f"/api/papers/{paper_id}/save")

    # ASSERT — ownership check fires before any state mutation
    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 for user B accessing user A's paper; "
        f"got {resp.status_code}: {resp.text[:200]}"
    )


async def test_pwst_07_state_transition_404_for_missing_paper(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/9999999/save returns 404 when the paper does not exist.

    # Verified: libs/jarvis_common/jarvis_common/db_helpers.py
    # (assert_paper_ownership returns an opaque 404 when no visible row exists).
    """
    # ACT — paper_id 9999999 does not exist in the test DB
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put("/api/papers/9999999/save")

    # ASSERT
    assert resp.status_code == 404, (
        f"Expected 404 for non-existent paper; got {resp.status_code}: {resp.text[:200]}"
    )


async def test_pwst_08_state_transition_audit_emits_event(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /api/papers/{id}/save: paper_user_state.updated_at advances on state transition.

    CONTRACT-GAP NOTE: save_paper does NOT emit to system_events (verified by
    grep 'system_events' in routers/papers.py → 0 matches).
    The observable audit trail is the trigger-maintained updated_at column on
    paper_user_state (set_updated_at_paper_user_state trigger, db/init.sql:1776).

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers.py:514
    # (save_paper calls _upsert_state_and_starred which executes INSERT ... ON CONFLICT
    # DO UPDATE SET state='to_read' → UPDATE path fires BEFORE UPDATE trigger
    # set_updated_at_paper_user_state → updated_at := NOW()).
    # Verified: db/init.sql:26-27 — set_updated_at() trigger function sets NEW.updated_at=NOW().
    """
    # ARRANGE — seed a paper with a paper_user_state row pinned to a past timestamp,
    # so the UPDATE caused by save_paper must advance updated_at beyond the pinned value.
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', 'PWST-08 Audit Trail Paper', ARRAY['Author'],
                   'https://pwst08.contract.test/audit', $2)
           RETURNING id""",
        "pwst08-audit-trail-ext",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    # Insert with an explicit past timestamp so we can detect the trigger advancing it.
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, updated_at)
           VALUES ($1, $2, 'inbox', NOW() - INTERVAL '1 hour')""",
        paper_id,
        contract_two_users.user_a_id,
    )
    before_row = await contract_conn.fetchrow(
        "SELECT updated_at FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    seed_updated_at = before_row["updated_at"]

    # ACT
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/save")

    # ASSERT
    assert resp.status_code in (200, 204), resp.text[:200]
    row = await contract_conn.fetchrow(
        "SELECT state, updated_at FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
        paper_id,
        contract_two_users.user_a_id,
    )
    assert row is not None, "paper_user_state row must exist after PUT /save"
    assert row["state"] == "to_read", f"Expected state='to_read'; got {row['state']!r}"
    assert row["updated_at"] is not None, "updated_at must not be NULL after state transition"
    # Trigger fires on UPDATE: updated_at must have advanced past the seeded past timestamp.
    assert row["updated_at"] > seed_updated_at, (
        f"updated_at must advance after state transition: "
        f"seed={seed_updated_at}, after={row['updated_at']}; "
        "set_updated_at_paper_user_state trigger not firing on state-transition UPDATE"
    )


# ---------------------------------------------------------------------------
# Audit-bug regression tests
#
# batch_save_papers must reject non-allowlisted pdf_url at persistence time.
# list_papers BM25 fallback uses websearch_to_tsquery consistently.
# delete_paper_feedback must call assert_paper_ownership before DELETE.
#
# Verified identifiers:
#   routers/papers_detail.py:batch_save_papers — pdf_url allowlist check before upsert
#   routers/papers_feed.py:list_papers — websearch_to_tsquery in BM25 fallback path
#   routers/papers_feedback.py:delete_paper_feedback — assert_paper_ownership before DELETE
# ---------------------------------------------------------------------------


async def test_batch_save_papers_rejects_non_allowlisted_pdf_url(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """POST /api/papers/batch-save clears pdf_url if domain not in ALLOWED_PDF_DOMAINS.

    Sends a paper with pdf_url pointing to evil.example.com (not in the allowlist).
    Asserts that the stored row has pdf_url=None (cleared at persistence time), confirming
    the SSRF allowlist is enforced during batch-save, not only during PDF download.

    # Verified: routers/papers_detail.py:batch_save_papers — pdf_url checked against
    # ALLOWED_PDF_DOMAINS from paper_ingestion.pdf_processor before upsert_paper call.
    """
    payload = [
        {
            "external_id": "w1d1004-contract-pdf-block-ext",
            "source_type": "arxiv",
            "title": "PDF Allowlist Contract Test",
            "authors": ["Test Author"],
            "url": "https://w1d1004.contract.test/paper",
            "pdf_url": "https://evil.example.com/malicious.pdf",
        }
    ]
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/papers/batch-save", json=payload)

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, list) and len(body) == 1, f"Expected list of 1; got {body!r}"
    saved_paper_id = body[0]["id"]

    # The stored pdf_url must be None — cleared by the allowlist check
    row = await contract_conn.fetchrow(
        "SELECT pdf_url FROM papers WHERE id = $1",
        saved_paper_id,
    )
    assert row is not None, f"Paper {saved_paper_id} not found in DB after batch-save"
    assert row["pdf_url"] is None, (
        f"pdf_url from evil.example.com must be cleared at persistence time; "
        f"got pdf_url={row['pdf_url']!r}"
    )


# ---------------------------------------------------------------------------
# batch-save attach claim check: naming an existing external_id must
# not grant the caller access to another tenant's private canonical row.
#
# batch_save_papers attaches the upserted row to the caller's library and
# echoes it only when the caller has a legitimate claim: is_insert (created it),
# the row is public (shared literature), or the caller is already a member
# (idempotent re-save). A collision with another tenant's private row is
# skipped — no attach (no PDF grant), no echo (no metadata leak), no enqueue.
#
# NOTE: the "A's row content unchanged" assertion below relies on the
# attach-only upsert (Lane 1A) that returns the victim's private row
# unmodified; both lanes are verified together under live-PG at GATE 1.
#
# Verified identifiers:
#   routers/papers_detail.py:batch_save_papers — claim check gates attach + echo
#   routers/pdfs.py:get_pdf — private paper requires user_library membership (404 otherwise)
# ---------------------------------------------------------------------------


async def test_batch_save_private_collision_denies_attach_echo_and_pdf(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """User B naming user A's private external_id gets no membership, no echo, 404 PDF.

    A saves external_id=X (lands private, A attached). B batch-saves the same X
    with distinct attacker title/abstract/metadata. Asserts (i) A's canonical row
    content + visibility_scope are unchanged (relies on Lane 1A attach-only upsert),
    (ii) B is not added to user_library for X, (iii) B's response omits X (no echo),
    (iv) GET /api/pdfs/{X} as B returns 404 (private, non-member).
    """
    external_id = "ten7-private-collision-ext"
    a_title = "Owner Private Paper A"
    a_payload = [
        {
            "external_id": external_id,
            "source_type": "arxiv",
            "title": a_title,
            "authors": ["Owner Author"],
            "abstract": "Owner abstract that must not leak.",
            "url": "https://ten7.contract.test/owner",
            "metadata": {"owner": True},
        }
    ]
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.post("/api/papers/batch-save", json=a_payload)
    assert resp_a.status_code == 200, f"A save failed: {resp_a.status_code}: {resp_a.text[:300]}"
    paper_x_id = resp_a.json()[0]["id"]

    # B attempts to collide on the same external_id with attacker content.
    b_payload = [
        {
            "external_id": external_id,
            "source_type": "arxiv",
            "title": "ATTACKER TITLE",
            "authors": ["Attacker"],
            "abstract": "attacker abstract",
            "url": "https://ten7.contract.test/attacker",
            "metadata": {"attacker": True},
        }
    ]
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.post("/api/papers/batch-save", json=b_payload)
        # (iv) B cannot read the private PDF: non-member on a private row => 404.
        pdf_resp = await c.get(f"/api/pdfs/{paper_x_id}")

    assert resp_b.status_code == 200, f"B save errored: {resp_b.status_code}: {resp_b.text[:300]}"
    # (iii) No metadata echo — the skipped collision yields an empty result list.
    assert resp_b.json() == [], f"B's response must not echo A's private row; got {resp_b.json()!r}"

    # (i) A's canonical row content + scope unchanged (Lane 1A attach-only upsert).
    row = await contract_conn.fetchrow(
        "SELECT title, abstract, visibility_scope, metadata FROM papers WHERE id = $1",
        paper_x_id,
    )
    assert row is not None
    assert row["title"] == a_title, f"A's title was overwritten by B: {row['title']!r}"
    assert row["visibility_scope"] == "private", (
        f"A's row must stay private; got {row['visibility_scope']!r}"
    )

    # (ii) B is NOT a member of A's paper.
    membership = await contract_conn.fetchrow(
        "SELECT 1 FROM user_library WHERE user_id = $1 AND paper_id = $2",
        contract_two_users.user_b_id,
        paper_x_id,
    )
    assert membership is None, "B must not be added to user_library for A's private paper"

    assert pdf_resp.status_code == 404, (
        f"B (non-member) must get 404 on A's private PDF; got {pdf_resp.status_code}"
    )


async def test_batch_save_public_collision_attaches_and_echoes(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """A public canonical row is shared: B batch-saving it is attached and echoed."""
    external_id = "ten7-public-shared-ext"
    paper_y_id = await contract_conn.fetchval(
        """INSERT INTO papers
               (external_id, source_type, title, authors, url, discovery_origin,
                visibility_scope)
           VALUES ($1, 'arxiv', 'Shared Public Paper Y', ARRAY['Public Author'],
                   'https://ten7.contract.test/public', 'pulse', 'public')
           RETURNING id""",
        external_id,
    )

    b_payload = [
        {
            "external_id": external_id,
            "source_type": "arxiv",
            "title": "Shared Public Paper Y",
            "authors": ["Public Author"],
            "url": "https://ten7.contract.test/public",
        }
    ]
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.post("/api/papers/batch-save", json=b_payload)

    assert resp_b.status_code == 200, f"B save failed: {resp_b.status_code}: {resp_b.text[:300]}"
    body = resp_b.json()
    assert [p["id"] for p in body] == [paper_y_id], (
        f"Public row must be echoed back to B; got {body!r}"
    )
    membership = await contract_conn.fetchrow(
        "SELECT 1 FROM user_library WHERE user_id = $1 AND paper_id = $2",
        contract_two_users.user_b_id,
        paper_y_id,
    )
    assert membership is not None, "B must be attached to the shared public paper"


async def test_batch_save_own_resave_is_idempotent(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Re-saving one's own private paper still returns it and preserves membership."""
    external_id = "ten7-own-resave-ext"
    payload = [
        {
            "external_id": external_id,
            "source_type": "arxiv",
            "title": "Own Resave Paper",
            "authors": ["Owner"],
            "url": "https://ten7.contract.test/resave",
        }
    ]
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        first = await c.post("/api/papers/batch-save", json=payload)
        assert first.status_code == 200, f"first save failed: {first.text[:300]}"
        paper_id = first.json()[0]["id"]
        second = await c.post("/api/papers/batch-save", json=payload)

    assert second.status_code == 200, f"resave failed: {second.status_code}: {second.text[:300]}"
    assert [p["id"] for p in second.json()] == [paper_id], (
        f"Own re-save must still echo the paper; got {second.json()!r}"
    )
    membership = await contract_conn.fetchrow(
        "SELECT 1 FROM user_library WHERE user_id = $1 AND paper_id = $2",
        contract_two_users.user_a_id,
        paper_id,
    )
    assert membership is not None, "Owner must remain a member after re-save"


async def test_list_papers_bm25_uses_websearch_to_tsquery(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """GET /api/papers?q=... BM25 fallback uses websearch_to_tsquery.

    Verifies behavioral consistency: a multi-term query that websearch_to_tsquery
    handles (e.g. quoted phrase OR operator) returns results without error, and
    user-library scoping is preserved. The embedder is None (BM25-only path).

    The test uses a sentinel paper with a unique title term so we can confirm
    the BM25 path executes the correct SQL parser without an error response.

    # Verified: routers/papers_feed.py:list_papers — websearch_to_tsquery('english', $N)
    # replaces the former plainto_tsquery in the BM25 fallback path.
    """
    sentinel = "xq7bm25websearchfix2026"

    bm25_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'arxiv', $2, ARRAY['Author'], 'https://w1d1008.test/bm25', $3)
           RETURNING id""",
        "w1d1008-bm25-websearch-ext",
        f"BM25 Websearch Fix Test {sentinel}",
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        bm25_paper_id,
    )
    await contract_conn.execute(
        "UPDATE papers SET search_vector = to_tsvector('english', title) WHERE id = $1",
        bm25_paper_id,
    )

    # BM25 path: embedder=None is already set on _pi_app_with_pool
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers", params={"q": sentinel})

    assert resp.status_code == 200, (
        f"BM25 websearch_to_tsquery path returned {resp.status_code}: {resp.text[:300]}"
    )
    ids = [p["id"] for p in resp.json()]
    assert bm25_paper_id in ids, (
        f"sentinel paper {bm25_paper_id} not returned by BM25 search for {sentinel!r}; ids={ids}"
    )


async def test_delete_paper_feedback_rejects_cross_owner(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """DELETE /api/papers/{paper_id}/feedback by non-owner returns 403/404.

    User A owns paper A and has a feedback row. User B attempts to DELETE that
    feedback row. assert_paper_ownership fires before the DELETE, so user B
    gets 403/404 and user A's row is untouched.

    # Verified: routers/papers_feedback.py:delete_paper_feedback —
    # assert_paper_ownership(conn, paper_id, user_id) called before DELETE FROM
    # recommendation_feedback.
    """
    paper_id = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id

    # Seed a feedback row for user A
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'positive', 'feed_thumbs')
           ON CONFLICT (paper_id, user_id, source) DO UPDATE SET signal='positive'""",
        paper_id,
        user_a_id,
    )

    # User B attempts to delete user A's feedback — must be rejected
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/papers/{paper_id}/feedback", params={"source": "feed_thumbs"})

    assert resp.status_code in (403, 404), (
        f"cross-owner DELETE /feedback returned {resp.status_code} "
        f"(expected 403 or 404); body: {resp.text[:300]}"
    )

    # User A's feedback row must still exist
    row = await contract_conn.fetchrow(
        "SELECT 1 FROM recommendation_feedback WHERE paper_id=$1 AND user_id=$2 AND source='feed_thumbs'",
        paper_id,
        user_a_id,
    )
    assert row is not None, "user A's feedback row was deleted by user B's cross-owner DELETE"


# ---------------------------------------------------------------------------
# POST /api/papers/process_batch — enqueues 202 + job_id
# ---------------------------------------------------------------------------


async def test_process_batch_enqueues_202_with_job_id(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/papers/process_batch returns 202 + job_id for an owned paper.

    Ownership fast-granted because paper_id_a is owned by user_a (discovered_by matches).
    The task_registry entry for papers.batch_process is patched so no worker is needed.

    # Verified: services/paper_ingestion/paper_ingestion/routers/papers_bulk.py:121
    # (process_batch: assert_papers_ownership then defer_async, returns JobCreateResponse).
    """
    from unittest.mock import AsyncMock, patch

    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"papers.batch_process": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post(
                "/api/papers/process_batch",
                json={"paper_ids": [contract_two_users.paper_id_a]},
            )

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("job_id"), f"Missing job_id: {body}"
    assert body.get("status") == "queued", f"Expected status=queued: {body}"
    mock_task.defer_async.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /api/papers/process-library — 202 envelope + skip contract
# ---------------------------------------------------------------------------


async def test_process_library_enqueues_202_with_job_id(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """POST /api/papers/process-library returns 202 + job_id when the caller's
    library has papers still needing a stage.

    # Verified: routers/papers_bulk.py:process_library — EXISTS pre-check then
    # defer_async, returns JobCreateResponse. paper_id_a is arxiv with
    # chunked_at NULL and sits in user_a's user_library (testing_db.py:814-827),
    # so the selection is non-empty.
    """
    from unittest.mock import AsyncMock, patch

    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"papers.process_library": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/papers/process-library")

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("job_id"), f"Missing job_id: {body}"
    assert body.get("status") == "queued", f"Expected status=queued: {body}"
    mock_task.defer_async.assert_awaited_once()


async def test_process_library_enqueues_bounded_reconciliation_when_chunks_exist(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """A chunked library still queues bounded vector reconciliation.

    # Verified: routers/papers_bulk.py:process_library returns
    # The database cannot prove Qdrant points exist, so chunked papers remain
    # bounded probe candidates for the background job.
    """
    from unittest.mock import AsyncMock, patch

    # user_a's only library paper becomes fully processed → selection is empty.
    await contract_conn.execute(
        "UPDATE papers SET chunked_at = now() WHERE id = $1",
        contract_two_users.paper_id_a,
    )

    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"papers.process_library": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/papers/process-library")

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert body.get("job_id"), f"Expected reconciliation job_id: {body}"
    assert body.get("status") == "queued", body
    mock_task.defer_async.assert_awaited_once()


# ---------------------------------------------------------------------------
# F10: daily_log.papers_read incremented by PUT /reading
# ---------------------------------------------------------------------------


async def test_f10_reading_increments_daily_log_papers_read(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """First PUT /reading (from to_read) increments daily_log.papers_read by 1.

    # Verified: paper_id_a starts in state='to_read' (testing_db.py:829-830).
    # daily_log.papers_read column: db/init.sql:547.
    # Conflict key: UNIQUE NULLS NOT DISTINCT (user_id, log_date) — init.sql:1477-1478.
    """
    paper_id = contract_two_users.paper_id_a
    user_id = contract_two_users.user_a_id

    # Baseline: capture papers_read before the call
    before = await contract_conn.fetchval(
        "SELECT COALESCE(papers_read, 0) FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
        user_id,
    )
    before = before or 0

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/reading")

    assert resp.status_code in (200, 204), resp.text[:200]

    after = await contract_conn.fetchval(
        "SELECT COALESCE(papers_read, 0) FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
        user_id,
    )
    assert after == before + 1, (
        f"daily_log.papers_read should be {before + 1} after first PUT /reading; got {after}"
    )


async def test_f10_re_reading_does_not_double_count(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """Re-marking an already-reading paper does NOT increment papers_read again.

    # Verified: allowed=('to_read','reading','done') permits re-mark; dedup must
    # check state_before='reading' and skip the increment in that case.
    """
    paper_id = contract_two_users.paper_id_a
    user_id = contract_two_users.user_a_id

    # First call: to_read → reading (should increment)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp1 = await c.put(f"/api/papers/{paper_id}/reading")
    assert resp1.status_code in (200, 204), resp1.text[:200]

    count_after_first = await contract_conn.fetchval(
        "SELECT COALESCE(papers_read, 0) FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
        user_id,
    )

    # Second call: reading → reading (same state, must NOT increment)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp2 = await c.put(f"/api/papers/{paper_id}/reading")
    assert resp2.status_code in (200, 204), resp2.text[:200]

    count_after_second = await contract_conn.fetchval(
        "SELECT COALESCE(papers_read, 0) FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
        user_id,
    )
    assert count_after_second == count_after_first, (
        f"Re-marking reading must not double-count papers_read: "
        f"after first={count_after_first}, after second={count_after_second}"
    )


async def test_f10_papers_read_scoped_to_acting_user(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """PUT /reading for user A does not affect user B's daily_log.papers_read.

    # Verified: daily_log is keyed (user_id, log_date); the upsert must bind user_a_id,
    # not bleed into user_b's row.
    """
    paper_id = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    b_before = await contract_conn.fetchval(
        "SELECT COALESCE(papers_read, 0) FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
        user_b_id,
    )
    b_before = b_before or 0

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/papers/{paper_id}/reading")
    assert resp.status_code in (200, 204), resp.text[:200]

    b_after = await contract_conn.fetchval(
        "SELECT COALESCE(papers_read, 0) FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
        user_b_id,
    )
    b_after = b_after or 0

    assert b_after == b_before, (
        f"User B's papers_read must be unchanged; before={b_before}, after={b_after}"
    )

    # Sanity: user A's row DID get incremented
    a_after = await contract_conn.fetchval(
        "SELECT COALESCE(papers_read, 0) FROM daily_log WHERE user_id=$1 AND log_date=CURRENT_DATE",
        user_a_id,
    )
    assert (a_after or 0) >= 1, f"User A's papers_read must be >=1; got {a_after}"
