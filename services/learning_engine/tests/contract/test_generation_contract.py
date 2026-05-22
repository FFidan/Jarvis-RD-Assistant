"""Generation + export contract tests — A198, A199, A200.

Idiomatic-mock carve-out applies:
- card_generator (LLM) and task_registry (procrastinate) kept as mocks per spec.
- anki_exporter (file generation) kept as mock.

Contract-tested behavior:
- A198 GET /api/export/anki/{deck_id}  — 404 for non-owner deck (exporter mocked)
- A199 POST /api/generate              — 404 for non-owner deck (task_registry mocked)
- A200 POST /api/generate/batch        — 404 for non-owner deck (task_registry mocked)
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import UTC, datetime, timedelta

from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "le-contract-generation-test-key"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app(contract_conn):
    from learning_engine.deps import get_anki_exporter, get_db_pool, get_fsrs_manager
    from learning_engine.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_fsrs = getattr(app.state, "fsrs_manager", None)
    original_exporter = getattr(app.state, "anki_exporter", None)
    original_generator = getattr(app.state, "card_generator", None)

    mock_fsrs = MagicMock()
    _now = datetime.now(UTC)
    mock_fsrs.create_new_card.return_value = ({}, _now)
    mock_fsrs.schedule_review.return_value = ({}, {}, _now + timedelta(days=1))

    # Idiomatic mock: anki_exporter returns a minimal .apkg bytes blob so the
    # export endpoint can stream a response when the deck is owned.
    mock_exporter = MagicMock()
    mock_exporter.export_deck.return_value = b"PK\x05\x06" + b"\x00" * 18

    app.state.db_pool = shared
    app.state.http_client = AsyncMock()
    app.state.fsrs_manager = mock_fsrs
    app.state.anki_exporter = mock_exporter
    app.state.card_generator = AsyncMock()
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_anki_exporter] = lambda: mock_exporter

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
# §A198 — GET /api/export/anki/{deck_id} — 404 for non-owner deck
# ---------------------------------------------------------------------------


async def test_export_anki_non_owner_deck_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot export user A's deck — 404 (IDOR guard).

    anki_exporter is kept mocked (file-generation boundary); the contract test
    exercises the real ``SELECT * FROM decks WHERE id = $1 AND user_id = $2``
    ownership check before the exporter is called.
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/export/anki/{deck_id_a}")

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} exporting user A's deck {deck_id_a} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A199 — POST /api/generate — 404 for non-owner deck (task_registry mocked)
# ---------------------------------------------------------------------------


async def test_generate_cards_non_owner_deck_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot enqueue generation for user A's deck — 404 (IDOR guard).

    task_registry (procrastinate defer_async) is kept mocked per idiomatic
    carve-out.  The contract exercises the real deck ownership check:
    ``SELECT id FROM decks WHERE id = $1 AND user_id = $2``.
    """
    import jarvis_common.task_registry as task_registry

    deck_id_a = contract_two_users.deck_id_a
    paper_id_a = contract_two_users.paper_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        async with _client(_le_app, contract_two_users.cookie_b) as c:
            resp = await c.post(
                "/api/generate",
                json={"paper_id": paper_id_a, "deck_id": deck_id_a, "max_cards": 3},
            )

    # RD-DA-001 fix: paper-ownership extractor now fires before deck-ownership,
    # so non-owner paper returns 403 instead of 404. Both are valid IDOR rejections.
    assert resp.status_code in (403, 404), (
        f"IDOR: user B got {resp.status_code} generating cards for user A's deck "
        f"{deck_id_a} (expected 403 or 404). Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# §A200 — POST /api/generate/batch — IDOR rejection for non-owner deck
# ---------------------------------------------------------------------------


async def test_batch_generate_non_owner_deck_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot enqueue batch generation for user A's deck — 404 (IDOR guard).

    task_registry kept mocked per idiomatic carve-out.  Contract exercises the
    real ``SELECT id FROM decks WHERE id = $1 AND user_id = $2`` check.
    """
    import jarvis_common.task_registry as task_registry

    deck_id_a = contract_two_users.deck_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate_batch": mock_task}):
        async with _client(_le_app, contract_two_users.cookie_b) as c:
            resp = await c.post(
                "/api/generate/batch",
                json={"deck_id": deck_id_a, "max_per_paper": 5},
            )

    # RD-DA-001 fix: ownership-related rejection may be 403 (paper-ownership)
    # or 404 (deck-ownership); both are valid IDOR rejections.
    assert resp.status_code in (403, 404), (
        f"IDOR: user B got {resp.status_code} batch-generating for user A's deck "
        f"{deck_id_a} (expected 403 or 404). Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# §A201 — generate_cards_core re-validates paper ownership (RD-DA-001 depth)
# ---------------------------------------------------------------------------


async def test_generate_cards_core_revalidates_paper_ownership(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """RD-DA-001 defense-in-depth: generate_cards_core raises 403 when the
    caller's user_id does not own the requested paper_id.

    This guards the worker entry point directly: even if a job were somehow
    enqueued with a mismatched paper_id, the core helper must re-validate
    ownership before reading paper data.

    Setup: seed a deck owned by user B so the deck check passes, then supply
    user A's paper_id so the paper ownership check fires and returns 403.
    """
    from fastapi import HTTPException

    from learning_engine.routers.generation import generate_cards_core

    paper_id_a = contract_two_users.paper_id_a
    user_b_id = contract_two_users.user_b_id

    # Seed a deck owned by user B within the contract transaction.
    deck_id_b = await contract_conn.fetchval(
        "INSERT INTO decks (user_id, name, description) VALUES ($1, $2, $3) RETURNING id",
        user_b_id,
        "contract-test-deck-b",
        "temp deck for RD-DA-001 test",
    )

    # Reach into the _le_app fixture to get the SharedConnPool already wired.
    shared = _le_app.state.db_pool

    with pytest.raises(HTTPException) as exc_info:
        await generate_cards_core(
            pool=shared,
            http_client=_le_app.state.http_client,
            paper_id=paper_id_a,
            deck_id=deck_id_b,
            max_cards=3,
            user_id=user_b_id,
        )

    assert exc_info.value.status_code == 403, (
        f"RD-DA-001 depth: generate_cards_core returned {exc_info.value.status_code} "
        f"instead of 403 for user_b accessing paper_a. Detail: {exc_info.value.detail}"
    )
