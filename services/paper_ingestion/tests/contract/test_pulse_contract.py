"""Pulse domain contract tests — D2 collapse.

Exercises real SQL against the contract DB (session-scoped Postgres +
per-test asyncpg transaction rollback).  Replaces:

- test_pulse_idor.py  (6 tests) — SQL-substring IDOR assertions replaced by
  behavioral contract: user B → 404 on A's pulse card, user B → 404 on rate
  (deck membership guard).
  Survivor-citation: test_pulse_router.py covers explain 200/404 paths and
  rate_card signal routing; contract layer adds real cross-user isolation claim.

- test_pulse_degradation.py (3 tests) — discover_candidates source-failure
  degradation is a subset of the richer coverage in test_pulse_discovery.py
  (22 tests, including rate_limit + 5xx + openalex-unconfigured paths).
  DROP; no new contract test needed (discovery is purely unit-level pipeline,
  not a DB-interaction boundary).

Idiomatic-mock carve-out (KEEP in this file and callers):
  - app.state.http_client — outbound HTTP boundary
  - app.state.embedder   — Ollama HTTP boundary
  - task_registry._TASK_MAP injection — procrastinate task boundary
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,  # opt out of _default_authenticated_user (returns user 1 globally)
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "pulse-contract-test-key-do-not-use-in-prod"


# ---------------------------------------------------------------------------
# App + client fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_pulse_app(contract_conn):
    """paper_ingestion app with db_pool wired to the contract connection.

    The limiter is disabled so rate-limit 429s never interfere with these
    ownership / IDOR assertions.
    """
    from unittest.mock import MagicMock

    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_embedder = getattr(app.state, "embedder", None)

    app.state.db_pool = shared
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()
    app.dependency_overrides[get_db_pool] = lambda: shared

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
        if original_embedder is None:
            if hasattr(app.state, "embedder"):
                del app.state.embedder
        else:
            app.state.embedder = original_embedder
        app.dependency_overrides.pop(get_db_pool, None)


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
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
# §D2-01 — Pulse IDOR: explain_card user isolation
# ---------------------------------------------------------------------------


async def test_explain_card_owner_gets_200(contract_two_users, _pi_pulse_app, _configure_api_key):
    """User A can fetch their own pulse card explain response (owner → 200)."""
    card_id = contract_two_users.pulse_card_id_a
    async with _client(_pi_pulse_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/pulse/explain/{card_id}")

    assert resp.status_code == 200, (
        f"Owner expected 200 for their own card {card_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    # Response shape: {card_id, reasoning, signals, llm_relevance, llm_novelty}
    assert body.get("card_id") == card_id


async def test_explain_card_user_b_gets_404(contract_two_users, _pi_pulse_app, _configure_api_key):
    """User B cannot access User A's pulse card — must get 404 (IDOR guard).

    Collapses test_pulse_idor.py::test_explain_card_filters_by_user_id_in_not_distinct_form
    and test_explain_card_filters_by_real_user_id.
    Stronger: exercises real pulse_decks.user_id JOIN rather than SQL-text assertions.
    """
    card_id = contract_two_users.pulse_card_id_a
    async with _client(_pi_pulse_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/pulse/explain/{card_id}")

    assert resp.status_code != 401, (
        f"GET /api/pulse/explain/{card_id}: got 401 — session wiring bug; "
        f"user B must authenticate before the ownership check fires"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} for user A's card {card_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §D2-01 — Pulse IDOR: rate_card deck-membership guard
# ---------------------------------------------------------------------------


async def test_rate_card_owner_gets_200(contract_two_users, _pi_pulse_app, _configure_api_key):
    """User A can rate a paper that IS in their deck (owner → 200).

    The seeded pulse_card links pulse_card_id_a's paper_id to pulse_deck_id_a,
    which is owned by user_a_id.  The 'open' rating is used so no write SQL
    fires on paper_user_state or recommendation_feedback (logging-only path).
    """
    # Retrieve paper_id from the seeded pulse_card row directly via contract_conn
    # is not available here — but the TwoUsers seed links pulse_card to paper_id_a.
    paper_id = contract_two_users.paper_id_a
    async with _client(_pi_pulse_app, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/pulse/rate", json={"paper_id": paper_id, "rating": "open"})

    assert resp.status_code == 200, (
        f"Owner expected 200 rating their own deck paper; got {resp.status_code}: {resp.text[:300]}"
    )


async def test_rate_card_user_b_gets_404(contract_two_users, _pi_pulse_app, _configure_api_key):
    """User B cannot rate User A's pulse-deck paper — must get 404.

    Collapses test_pulse_idor.py::test_rate_card_deck_guard_filters_by_user_id,
    test_rate_card_deck_guard_with_real_user_id, and
    test_pulse_idor.py::test_rate_card_membership_guard_returns_404_when_paper_not_in_deck.
    Stronger: exercises real pulse_cards JOIN pulse_decks WHERE pd.user_id = $2
    rather than fetchval SQL-text + param-order assertions.
    """
    paper_id = contract_two_users.paper_id_a
    async with _client(_pi_pulse_app, contract_two_users.cookie_b) as c:
        resp = await c.post("/api/pulse/rate", json={"paper_id": paper_id, "rating": "up"})

    assert resp.status_code != 401, (
        "POST /api/pulse/rate: got 401 — session wiring bug; "
        "user B must authenticate before the membership guard fires"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} trying to rate user A's deck paper {paper_id} "
        f"(expected 404 — paper not in B's deck). Body: {resp.text[:300]}"
    )


async def test_rate_card_paper_not_in_any_deck_returns_404(
    contract_two_users, _pi_pulse_app, _configure_api_key
):
    """Any user rating a paper that's not in their deck returns 404.

    Collapses test_pulse_idor.py::test_rate_card_membership_guard_returns_404_when_paper_not_in_deck
    (user_id=42 path; uses real DB — paper_id 999999 never exists in the seeded schema).
    """
    async with _client(_pi_pulse_app, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/pulse/rate", json={"paper_id": 999999, "rating": "up"})

    assert resp.status_code == 404, (
        f"Expected 404 for paper not in any deck; got {resp.status_code}: {resp.text[:300]}"
    )
