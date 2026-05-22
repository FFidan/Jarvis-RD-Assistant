"""Learning Engine domain contract tests — D6 collapse.

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

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import UTC, datetime, timedelta

from jarvis_common.testing import SharedConnPool

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "le-contract-test-key-do-not-use-in-prod"


# ---------------------------------------------------------------------------
# App fixture — wires LE app to the contract connection
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app(contract_conn):
    """learning_engine app with db_pool wired to the contract connection.

    The limiter is disabled so rate-limit 429s never interfere with ownership /
    IDOR assertions.  The FSRSManager, AnkiExporter, and http_client are
    idiomatic mocks (algorithmic / outbound-HTTP boundaries kept as mocks per
    idiomatic-mock carve-out).
    """
    from learning_engine.deps import get_db_pool, get_fsrs_manager, get_anki_exporter
    from learning_engine.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_fsrs = getattr(app.state, "fsrs_manager", None)
    original_exporter = getattr(app.state, "anki_exporter", None)
    original_generator = getattr(app.state, "card_generator", None)

    # Idiomatic mocks for non-DB state
    mock_fsrs = MagicMock()
    _now = datetime.now(UTC)
    mock_fsrs.create_new_card.return_value = ({}, _now)
    mock_fsrs.schedule_review.return_value = ({}, {}, _now + timedelta(days=1))

    app.state.db_pool = shared
    app.state.http_client = AsyncMock()
    app.state.fsrs_manager = mock_fsrs
    app.state.anki_exporter = MagicMock()
    app.state.card_generator = AsyncMock()
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_anki_exporter] = lambda: MagicMock()

    from learning_engine.deps import limiter

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False

    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
        if original_pool is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original_pool
        if original_http is None:
            if hasattr(app.state, "http_client"):
                del app.state.http_client
        else:
            app.state.http_client = original_http
        if original_fsrs is None:
            if hasattr(app.state, "fsrs_manager"):
                del app.state.fsrs_manager
        else:
            app.state.fsrs_manager = original_fsrs
        if original_exporter is None:
            if hasattr(app.state, "anki_exporter"):
                del app.state.anki_exporter
        else:
            app.state.anki_exporter = original_exporter
        if original_generator is None:
            if hasattr(app.state, "card_generator"):
                del app.state.card_generator
        else:
            app.state.card_generator = original_generator
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_fsrs_manager, None)
        app.dependency_overrides.pop(get_anki_exporter, None)


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    """Configure the JARVIS_API_KEY in the test environment."""
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


def _client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


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
