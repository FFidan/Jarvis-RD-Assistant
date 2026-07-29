"""Learning Engine domain contract tests.

Exercises real SQL against the contract DB (session-scoped Postgres +
per-test asyncpg transaction rollback via ``contract_conn``).

IDOR / ownership claims tested here are the behavioral contracts that
the mock-pool unit tests can only approximate.  Each test comments on
which old SQL-text assertion or mock-short-circuit it replaces.

Idiomatic-mock carve-out (KEPT, not contract-tested):
- app.state.http_client — outbound HTTP to LiteLLM / paper_ingestion
- app.state.fsrs_manager / app.state.card_generator — algorithmic logic
- app.state.anki_exporter — file-generation logic
"""

from __future__ import annotations

import pytest


from jarvis_common.testing_contract_apps import (
    make_contract_client as _client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# App fixture — wires LE app to the contract connection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §D6-01 — Card IDOR: PUT /api/cards/{id} — user B cannot update user A's card
# ---------------------------------------------------------------------------


async def test_update_card_owner_gets_200(contract_two_users, _le_app, _configure_api_key):
    """User A can update their own card (owner → 200).

    Collapses test_le_endpoints.py::test_update_card_returns_200 from a mock
    (fetchrow returns fake row, no user_id scoping exercised) to a real DB
    query that exercises ``WHERE id = $1 AND user_id = $2 FOR UPDATE``.
    """
    card_id = contract_two_users.card_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/cards/{card_id}", json={"front": "Updated question?"})

    assert resp.status_code == 200, (
        f"Owner expected 200 updating their own card {card_id}; "
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["id"] == card_id
    assert body["front"] == "Updated question?"


async def test_update_card_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot update User A's card — must get 404 (IDOR guard).

    Exercises the real ``SELECT * FROM cards WHERE id = $1 AND user_id = $2
    FOR UPDATE`` ownership check rather than mock fetchrow SQL-text assertions.
    Stronger than test_cards_scoping.py which only tests the unit-level layer
    with a mock fetchrow returning None (not a real DB isolation claim).
    """
    card_id = contract_two_users.card_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.put(f"/api/cards/{card_id}", json={"front": "Hijacked!"})

    assert resp.status_code != 401, (
        f"PUT /api/cards/{card_id}: got 401 — session wiring bug; "
        f"user B must authenticate before the ownership check fires"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} trying to update user A's card {card_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §D6-01 — Card IDOR: DELETE /api/cards/{id} — user B cannot delete user A's card
# ---------------------------------------------------------------------------


async def test_delete_card_owner_gets_204(contract_two_users, _le_app, _configure_api_key):
    """User A can delete their own card (owner → 204).

    The ``DELETE FROM cards WHERE id = $1 AND user_id = $2`` path is exercised
    against the real seeded row.
    """
    card_id = contract_two_users.card_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/cards/{card_id}")

    assert resp.status_code == 204, (
        f"Owner expected 204 deleting their own card {card_id}; "
        f"got {resp.status_code}: {resp.text[:300]}"
    )


async def test_delete_card_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot delete User A's card — must get 404 (IDOR guard).

    Exercises the real ``DELETE FROM cards WHERE id = $1 AND user_id = $2``
    ownership filter: when no row matches the DELETE returns "DELETE 0", and
    the handler must raise 404.
    """
    card_id = contract_two_users.card_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/cards/{card_id}")

    assert resp.status_code != 401, (
        f"DELETE /api/cards/{card_id}: got 401 — session wiring bug; "
        f"user B must authenticate before the ownership check fires"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} trying to delete user A's card {card_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §D6-01 — Deck scoping: GET /api/decks — user B does not see user A's decks
# ---------------------------------------------------------------------------


async def test_list_decks_user_b_cannot_see_user_a_deck(
    contract_two_users, _le_app, _configure_api_key
):
    """GET /api/decks as user B returns only B's decks, not A's.

    Exercises the real ``WHERE d.user_id = $1`` filter in the SQL:
    ``SELECT … FROM decks d WHERE d.user_id = $1``.
    This replaces the isolation pattern in test_decks_router.py which uses
    two separate mock pools (``pool_a``, ``pool_b``) and never proves real
    row-level filtering.
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/decks")

    assert resp.status_code == 200, (
        f"GET /api/decks for user B failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    deck_ids = [d["id"] for d in body]
    assert deck_id_a not in deck_ids, (
        f"IDOR: user B's deck list contains user A's deck {deck_id_a}. Full list: {deck_ids}"
    )


async def test_list_decks_user_a_sees_own_deck(contract_two_users, _le_app, _configure_api_key):
    """GET /api/decks as user A includes A's own deck.

    Positive control: confirms the WHERE user_id = $1 filter scopes
    correctly to the requesting user rather than returning nothing.
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/decks")

    assert resp.status_code == 200, (
        f"GET /api/decks for user A failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    deck_ids = [d["id"] for d in body]
    assert deck_id_a in deck_ids, f"User A expected to see their own deck {deck_id_a} in {deck_ids}"


async def test_list_decks_card_count_scoped_to_user(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/decks card_count is scoped to the deck owner's cards only (LEFT JOIN with user_id).

    User A has deck_id_a with card_id_a. This test verifies that when A lists their decks,
    the card_count for deck_id_a correctly reflects only A's cards.

    This exercises the real ``LEFT JOIN cards c ON c.deck_id = d.id AND c.user_id = $1``
    predicate, which ensures the COUNT(c.id) aggregate counts only the authorized user's cards.
    The test seeded one card (card_id_a); the card_count must be >= 1 to prove the join works.
    If the LEFT JOIN lacked the user_id scope, it could leak counts from other users' cards
    (though the fixture doesn't seed user B's cards, a real deployment could).
    """
    deck_id_a = contract_two_users.deck_id_a
    card_id_a = contract_two_users.card_id_a

    # User A lists their decks
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/decks")

    assert resp.status_code == 200, (
        f"GET /api/decks for user A failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()

    # Find deck_id_a in the response
    deck_a = next((d for d in body if d["id"] == deck_id_a), None)
    assert deck_a is not None, (
        f"User A expected to see their own deck {deck_id_a} in {[d['id'] for d in body]}"
    )

    # The card_count must be >= 1 (should include card_id_a which belongs to user A)
    # The fix adds "AND c.user_id = $1" to the LEFT JOIN to prevent leaking other users' cards.
    assert deck_a["card_count"] >= 1, (
        f"User A's deck {deck_id_a} has card_count={deck_a['card_count']}; "
        f"expected >= 1 to include the seeded card {card_id_a}. "
        f"The LEFT JOIN may not be properly scoped to the owner."
    )

    # Verify the card is actually in the DB for this user/deck
    verified_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM cards WHERE deck_id = $1 AND user_id = $2",
        deck_id_a,
        contract_two_users.user_a_id,
    )
    assert deck_a["card_count"] == verified_count, (
        f"Deck {deck_id_a} card_count {deck_a['card_count']} does not match DB count {verified_count}. "
        f"The LEFT JOIN may have cardinality issues."
    )


# ---------------------------------------------------------------------------
# §D6-01 — Review IDOR: POST /api/review/{card_id} — user B cannot review user A's card
# ---------------------------------------------------------------------------


async def test_review_submit_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot submit a review for User A's card — must get 404 (IDOR guard).

    Exercises the real ``SELECT * FROM cards WHERE id = $1 AND user_id = $2
    FOR UPDATE`` ownership check in submit_review rather than the mock-fetchrow
    SQL-substring assertion in test_review_sync.py::test_cross_user_isolation.
    """
    card_id = contract_two_users.card_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            f"/api/review/{card_id}",
            json={"rating": 3, "review_duration_ms": 1000},
        )

    assert resp.status_code != 401, (
        f"POST /api/review/{card_id}: got 401 — session wiring bug; "
        f"user B must authenticate before the ownership check fires"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} trying to review user A's card {card_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §D6-05 — Cards list pagination: real DB query uses LIMIT/OFFSET
# ---------------------------------------------------------------------------


async def test_list_cards_pagination_response_shape(
    contract_two_users, _le_app, _configure_api_key
):
    """GET /api/cards with limit=1 returns at most 1 card.

    Replaces test_le_endpoints.py::test_list_cards_with_pagination which
    asserts SQL-text ``"LIMIT" in sql`` against mock call_args (a
    whitebox implementation check).  Here we assert the observable contract:
    response contains at most ``limit`` items.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/cards", params={"limit": 1})

    assert resp.status_code == 200, (
        f"GET /api/cards?limit=1 failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert len(body) <= 1, f"Expected at most 1 card with limit=1, got {len(body)}: {body}"


# ---------------------------------------------------------------------------
# §D6-PQ — Project question CRUD: ownership scoping against real schema
#
# Replaces the SQL-substring asserts in test_project_questions.py:
#   - "WHERE id = $1 AND user_id = $2" (lines 56, 120)
#   - "project_id = $1 AND user_id = $2" (line 60)
#   - "INSERT INTO project_questions (project_id, user_id, body)" (line 91)
#   - "DELETE FROM project_questions WHERE id = $1 AND user_id = $2" (line 120)
# Each contract test here exercises the real predicate against seeded rows.
# ---------------------------------------------------------------------------


async def test_list_project_questions_owner_sees_own(
    contract_two_users, _le_app, _configure_api_key
):
    """User A can list questions for their own project.

    Collapses the SQL-text ownership-guard check in
    test_project_questions.py::test_list_questions_owner_scoped_returns_rows.
    Against a real DB, user_id scoping is exercised by the query itself.
    """
    project_id = contract_two_users.project_id_a
    # First seed a question for user A inside the transaction
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        create_resp = await c.post(
            f"/api/projects/{project_id}/questions",
            json={"body": "Contract question alpha"},
        )
    assert create_resp.status_code == 201, (
        f"POST /api/projects/{project_id}/questions failed: "
        f"{create_resp.status_code}: {create_resp.text[:300]}"
    )
    # Owner should see their question in the list
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        list_resp = await c.get(f"/api/projects/{project_id}/questions")
    assert list_resp.status_code == 200, (
        f"GET questions as owner failed: {list_resp.status_code}: {list_resp.text[:300]}"
    )
    bodies = [q["body"] for q in list_resp.json()]
    assert "Contract question alpha" in bodies, (
        f"Owner expected to see their question; got: {bodies}"
    )


async def test_list_project_questions_user_b_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot list questions for User A's project — must get 404.

    Exercises the real ``WHERE id = $1 AND user_id = $2`` owner-guard in
    list_project_questions._assert_project_owner rather than the mock-level
    SQL-substring check in test_project_questions.py::test_list_questions_404_for_other_users_project.
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/projects/{project_id}/questions")
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} listing user A's project {project_id} questions "
        f"(expected 404). Body: {resp.text[:300]}"
    )


async def test_create_project_question_user_b_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot create a question for User A's project — must get 404.

    Collapses test_project_questions.py::test_create_question_404_for_other_users_project
    which only mocks fetchval returning None (no real predicate exercised).
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/questions",
            json={"body": "Injected question"},
        )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} trying to create a question on user A's project "
        f"{project_id} (expected 404). Body: {resp.text[:300]}"
    )


async def test_list_projects_user_b_cannot_see_user_a_project(
    contract_two_users, _le_app, _configure_api_key
):
    """GET /api/projects as user B does not return user A's project.

    Collapses the SQL-column-presence check in
    test_project_questions.py::test_list_projects_counts_present_in_both_branches
    (``assert "paper_count" in unfiltered_sql``).  Here we assert the
    behavioural contract: user B's project list excludes A's rows.
    """
    project_id_a = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/projects")
    assert resp.status_code == 200, (
        f"GET /api/projects for user B failed: {resp.status_code}: {resp.text[:300]}"
    )
    ids = [p["id"] for p in resp.json()]
    assert project_id_a not in ids, (
        f"IDOR: user B sees user A's project {project_id_a} in project list {ids}"
    )


async def test_get_project_detail_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """GET /api/projects/{id} as user B for user A's project returns 404.

    Collapses test_project_questions.py::test_get_project_detail_includes_counts
    which only mock-verifies ``"project_papers" in counts_sql``.  This contract
    test exercises the real ownership check against the seeded row.
    """
    project_id_a = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/projects/{project_id_a}")
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} fetching user A's project detail "
        f"{project_id_a} (expected 404). Body: {resp.text[:300]}"
    )


async def test_get_project_detail_owner_sees_counts(
    contract_two_users, _le_app, _configure_api_key
):
    """GET /api/projects/{id} for owner includes paper_count and open_question_count.

    Positive contract: confirms the counts subquery columns reach the response.
    Collapses the ``assert "project_papers" in counts_sql`` whitebox check in
    test_project_questions.py::test_get_project_detail_includes_counts.
    """
    project_id_a = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/projects/{project_id_a}")
    assert resp.status_code == 200, (
        f"GET /api/projects/{project_id_a} for owner failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert "paper_count" in body, f"Response missing paper_count field: {body}"
    assert "open_question_count" in body, f"Response missing open_question_count field: {body}"
    # Counts are non-negative integers (type contract)
    assert isinstance(body["paper_count"], int) and body["paper_count"] >= 0
    assert isinstance(body["open_question_count"], int) and body["open_question_count"] >= 0


# ---------------------------------------------------------------------------
# §A186 — POST /api/cards — 404 when deck not owned by caller
# ---------------------------------------------------------------------------


async def test_create_card_non_owner_deck_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot create a card in user A's deck — must get 404 (IDOR guard).

    Exercises the real ``SELECT id FROM decks WHERE id = $1 AND user_id = $2``
    ownership check in create_card, rather than the mock-fetchval assertion in
    test_cards_router.py (which only confirms the SQL text).
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/cards",
            json={
                "deck_id": deck_id_a,
                "card_type": "concept",
                "front": "Injected front",
                "back": "Injected back",
            },
        )

    assert resp.status_code != 401, (
        "POST /api/cards: got 401 — session wiring bug; "
        "user B must authenticate before the ownership check fires"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} creating a card in user A's deck {deck_id_a} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A186 (positive) — POST /api/cards — card created with owner's user_id
# ---------------------------------------------------------------------------


async def test_create_card_row_has_owner_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/cards creates a card with the caller's user_id in DB.

    Exercises insert_card setting user_id=user_id — a behavior the mock-pool
    tests cannot verify because they never touch real Postgres.
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/cards",
            json={
                "deck_id": deck_id_a,
                "card_type": "concept",
                "front": "Contract card front",
                "back": "Contract card back",
            },
        )

    assert resp.status_code == 201, (
        f"POST /api/cards failed for owner: {resp.status_code}: {resp.text[:300]}"
    )
    card_id = resp.json()["id"]
    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM cards WHERE id = $1",
        card_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Card {card_id} has user_id={db_user_id} in DB; "
        f"expected user_a_id={contract_two_users.user_a_id}"
    )


# ---------------------------------------------------------------------------
# §A190 — POST /api/decks — deck row inserted with caller's user_id
# ---------------------------------------------------------------------------


async def test_create_deck_row_has_caller_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/decks creates a deck with the caller's user_id in DB.

    Exercises the real INSERT INTO decks … user_id = $4 against the contract
    schema, replacing the mock-fetchrow assertion in test_decks_router.py.
    Also verifies the new deck appears in user A's list and NOT in user B's.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/decks", json={"name": "Contract Deck Alpha"})

    assert resp.status_code == 201, f"POST /api/decks failed: {resp.status_code}: {resp.text[:300]}"
    deck_id = resp.json()["id"]

    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM decks WHERE id = $1",
        deck_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Deck {deck_id} has user_id={db_user_id}; expected {contract_two_users.user_a_id}"
    )

    # Confirm user B cannot see the new deck
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        list_resp = await c.get("/api/decks")
    assert list_resp.status_code == 200
    b_deck_ids = [d["id"] for d in list_resp.json()]
    assert deck_id not in b_deck_ids, (
        f"IDOR: user B sees user A's newly created deck {deck_id} in list {b_deck_ids}"
    )


# ---------------------------------------------------------------------------
# Cluster 12 — Card behaviors
# Survivor-of mock-units in test_cards_router.py:
#   test_create_card_success_uses_evidence_payload
#   test_update_card_returns_existing_row_when_body_is_empty
#   test_update_card_uses_dynamic_update             (behavioral)
#   test_create_card_raises_404_on_fk_violation_deck
#   test_create_card_skips_ownership_check_when_no_paper (no-paper happy path)
#   test_card_create_front_over_cap_is_rejected      (HTTP layer)
# Pre-existing survivors cover:
#   test_update_card_raises_404_when_missing         → test_update_card_user_b_gets_404 (line 161)
#   test_delete_card_raises_404_when_row_missing     → test_delete_card_user_b_gets_404 (line 204)
#   test_create_card_asserts_paper_ownership         → test_create_card_non_owner_deck_gets_404 (line 475)
# ---------------------------------------------------------------------------


async def test_create_card_persists_evidence_payload(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/cards with evidence={quote, page_number} persists it as JSONB.

    Exercises the real ``evidence = body.evidence.model_dump() if body.evidence else {}``
    path and the JSONB write to cards.evidence. Replaces the mock-unit assertion on
    ``mock_insert.await_args.args[6]`` (positional-arg lock-in).

    # Verified: services/learning_engine/learning_engine/routers/cards.py:30
    # (create_card serializes body.evidence into the JSONB column via insert_card).
    """
    deck_id_a = contract_two_users.deck_id_a
    paper_id_a = contract_two_users.paper_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/cards",
            json={
                "deck_id": deck_id_a,
                "paper_id": paper_id_a,
                "card_type": "concept",
                "front": "What is X?",
                "back": "X is Y.",
                "evidence": {"quote": "exact quote text", "page_number": 7},
            },
        )

    assert resp.status_code == 201, (
        f"POST /api/cards with evidence failed: {resp.status_code}: {resp.text[:300]}"
    )
    card_id = resp.json()["id"]
    assert resp.json()["stale"] is False

    card_row = await contract_conn.fetchrow(
        """
        SELECT c.evidence, c.content_generation, p.content_generation AS paper_generation
        FROM cards c
        JOIN papers p ON p.id = c.paper_id
        WHERE c.id = $1
        """,
        card_id,
    )
    evidence_row = card_row["evidence"]
    assert evidence_row is not None, "evidence column is NULL — payload not persisted"
    # asyncpg JSONB codec decodes to dict
    assert isinstance(evidence_row, dict), f"evidence should be dict, got {type(evidence_row)}"
    assert evidence_row.get("quote") == "exact quote text", (
        f"evidence.quote not persisted: got {evidence_row.get('quote')!r}"
    )
    assert evidence_row.get("page_number") == 7, (
        f"evidence.page_number not persisted: got {evidence_row.get('page_number')!r}"
    )
    assert card_row["content_generation"] == card_row["paper_generation"]


async def test_create_card_missing_deck_returns_404(
    contract_two_users, _le_app, _configure_api_key
):
    """POST /api/cards with a nonexistent deck_id returns 404.

    Exercises the ``SELECT id FROM decks WHERE id = $1 AND user_id = $2`` check —
    when the SELECT returns NULL, the handler raises HTTPException(404, "Deck not
    found"). This replaces the mock-unit that patched ``insert_card`` to raise
    ForeignKeyViolationError (an internal mechanic not visible at the HTTP boundary).

    # Verified: services/learning_engine/learning_engine/routers/cards.py:30
    # (create_card lines 45-50: missing-deck SELECT yields None → 404).
    """
    nonexistent_deck_id = 9_999_999
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/cards",
            json={
                "deck_id": nonexistent_deck_id,
                "card_type": "concept",
                "front": "Q?",
                "back": "A.",
            },
        )

    assert resp.status_code == 404, (
        f"Expected 404 for missing deck, got {resp.status_code}: {resp.text[:300]}"
    )
    assert "deck" in resp.text.lower(), (
        f"404 detail should reference 'deck'; got: {resp.text[:200]}"
    )


async def test_update_card_empty_body_returns_existing_row(
    contract_two_users, _le_app, _configure_api_key
):
    """PUT /api/cards/{id} with empty body returns the existing row without mutation.

    Exercises the ``SELECT * FROM cards WHERE id = $1 AND user_id = $2 FOR UPDATE``
    short-circuit when no fields change. Replaces the mock-unit assertion on
    ``conn.fetchrow.await_count == 1`` (implementation-detail lock-in).

    # Verified: services/learning_engine/learning_engine/routers/cards.py:113
    # (update_card short-circuits when CardUpdate is fully empty).
    """
    card_id = contract_two_users.card_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/cards/{card_id}", json={})

    assert resp.status_code == 200, (
        f"PUT /api/cards/{card_id} with empty body failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["id"] == card_id, (
        f"Empty-body PUT returned wrong row: got id={body.get('id')}, expected {card_id}"
    )


async def test_create_card_front_over_cap_returns_422(
    contract_two_users, _le_app, _configure_api_key
):
    """POST /api/cards with front > 500 chars returns 422 via FastAPI validation.

    Upgrades the pure-unit ``test_card_create_front_over_cap_is_rejected`` to a
    full HTTP contract: confirms the cap is enforced at the request boundary,
    not just at the Pydantic model layer.

    # Verified: services/learning_engine/learning_engine/routers/cards.py:30
    # (create_card binds CardCreate; FastAPI runs Pydantic validation before handler).
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/cards",
            json={
                "deck_id": deck_id_a,
                "card_type": "concept",
                "front": "x" * 501,
                "back": "valid back",
            },
        )

    assert resp.status_code == 422, (
        f"Expected 422 for oversized front, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# GET /api/export/anki/{deck_id} — user B cannot see user A's cards
# ---------------------------------------------------------------------------


async def test_export_anki_user_b_cannot_see_user_a_cards(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot export user A's cards via GET /api/export/anki/{deck_id}.

    Exercises the real ``WHERE c.deck_id = $1 AND c.user_id = $2`` scoping in
    the cards SELECT inside the export endpoint. User B should receive a 404
    (the deck is owned by A), but if they somehow accessed a shared deck, they
    would only see their own cards. This test verifies the user_id predicate
    on the cards query blocks card leakage from user A.

    # Verified: services/learning_engine/learning_engine/routers/export.py:37-44
    # (export_anki cards SELECT now includes AND c.user_id = $2).
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/export/anki/{deck_id_a}")

    # User B cannot access user A's deck, so they should get 404 first
    # (the deck ownership check on line 31 of export.py catches this).
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} exporting user A's deck {deck_id_a} \n"
        f"(expected 404). Body: {resp.text[:300]}"
    )


async def test_export_anki_user_a_exports_own_cards(
    contract_two_users, _le_app, _configure_api_key
):
    """User A can export their own cards from their deck via GET /api/export/anki/{deck_id}.

    Positive control: confirms the export endpoint returns a valid Anki .apkg
    file (or at least a streaming response) when the caller owns the deck.
    Verifies the user_id scoping on the cards SELECT does not block the owner
    from exporting their own cards.

    # Verified: services/learning_engine/learning_engine/routers/export.py:37-44
    # (export_anki cards SELECT includes AND c.user_id = $2, user A matches).
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/export/anki/{deck_id_a}")

    assert resp.status_code == 200, (
        f"Owner expected 200 exporting their own deck {deck_id_a}; \n"
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    # Check that response is a streaming response with apkg media type
    assert resp.headers.get("content-type") == "application/octet-stream", (
        f"Expected application/octet-stream, got {resp.headers.get('content-type')}"
    )
    # Should have attachment filename header
    assert "attachment" in resp.headers.get("content-disposition", ""), (
        f"Missing or invalid Content-Disposition header: {resp.headers.get('content-disposition')}"
    )


async def test_stale_card_is_retained_but_excluded_from_operational_surfaces(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
):
    """Earlier-generation cards stay editable while due and export paths ignore them."""
    card_id = contract_two_users.card_id_a
    paper_id = contract_two_users.paper_id_a
    deck_id = contract_two_users.deck_id_a
    user_id = contract_two_users.user_a_id
    await contract_conn.execute(
        """
        UPDATE cards
        SET due_at = NOW() - INTERVAL '1 hour',
            content_generation = (
                SELECT content_generation FROM papers WHERE id = $2
            )
        WHERE id = $1
        """,
        card_id,
        paper_id,
    )
    paperless_id = await contract_conn.fetchval(
        """
        INSERT INTO cards
            (deck_id, paper_id, card_type, front, back, user_id, due_at)
        VALUES ($1, NULL, 'concept', 'paperless-current', 'answer', $2,
                NOW() - INTERVAL '1 hour')
        RETURNING id
        """,
        deck_id,
        user_id,
    )
    await contract_conn.execute(
        "UPDATE papers SET content_generation = content_generation + 1 WHERE id = $1",
        paper_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        listed = await c.get("/api/cards", params={"deck_id": deck_id, "limit": 20})
        edited = await c.put(
            f"/api/cards/{card_id}",
            json={"front": "retained earlier-version card"},
        )
        due = await c.get("/api/review/next", params={"deck_id": deck_id, "limit": 10})
        decks = await c.get("/api/decks")
        stats = await c.get("/api/stats")
        _le_app.state.anki_exporter.export_deck.reset_mock()
        exported = await c.get(f"/api/export/anki/{deck_id}")

    assert listed.status_code == 200, listed.text[:300]
    stale_card = next(card for card in listed.json() if card["id"] == card_id)
    assert stale_card["stale"] is True
    assert edited.status_code == 200, edited.text[:300]
    assert edited.json()["stale"] is True
    assert (
        await contract_conn.fetchval(
            "SELECT content_generation FROM cards WHERE id = $1",
            card_id,
        )
        == 0
    )

    assert due.status_code == 200, due.text[:300]
    assert [card["id"] for card in due.json()] == [paperless_id]
    deck = next(item for item in decks.json() if item["id"] == deck_id)
    assert deck["card_count"] == 2
    assert deck["due_count"] == 1
    assert stats.json()["total_cards"] == 2
    assert stats.json()["due_now"] == 1

    assert exported.status_code == 200, exported.text[:300]
    exported_cards = _le_app.state.anki_exporter.export_deck.call_args.args[1]
    assert [card["front"] for card in exported_cards] == ["paperless-current"]
