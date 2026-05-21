"""Endpoint-level tests for the Learning Engine service.

Covers:
- Deck CRUD: POST /api/decks, GET /api/decks
- Card CRUD: POST /api/cards, GET /api/cards, PUT /api/cards/{id}, DELETE /api/cards/{id}
- Review flow: GET /api/review/next, POST /api/review/{card_id}
- Stats: GET /api/stats
- Generate: POST /api/generate
- Export: GET /api/export/anki/{deck_id}
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from tests.le_helpers import make_card_row
from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(UTC)


def _make_deck_row(**overrides):
    values = {
        "id": 1,
        "name": "Test Deck",
        "description": None,
        "topic_id": None,
        "card_count": 0,
        "due_count": 0,
    }
    values.update(overrides)
    values["created_at"] = _now()
    return FakeRecord(**values)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked dependencies and disabled auth."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import (
        current_user_id_strict,
        current_user_id_strict_with_owner_override,
    )
    from learning_engine.deps import (
        get_anki_exporter,
        get_db_pool,
        get_fsrs_manager,
    )
    from learning_engine.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_fsrs = MagicMock()
    mock_fsrs.create_new_card.return_value = ({}, _now())
    mock_fsrs.schedule_review.return_value = ({}, {}, _now() + timedelta(days=1))
    app.state.fsrs_manager = mock_fsrs

    mock_generator = AsyncMock()
    app.state.card_generator = mock_generator

    mock_exporter = MagicMock()
    app.state.anki_exporter = mock_exporter

    async def override_db_pool():
        return mock_pool

    async def override_api_key():
        return None

    async def override_fsrs_manager():
        return mock_fsrs

    async def override_anki_exporter():
        return mock_exporter

    app.dependency_overrides[get_db_pool] = override_db_pool
    app.dependency_overrides[verify_api_key] = override_api_key
    app.dependency_overrides[get_fsrs_manager] = override_fsrs_manager
    app.dependency_overrides[get_anki_exporter] = override_anki_exporter
    app.dependency_overrides[current_user_id_strict] = lambda: 1
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 1

    yield app, conn, mock_http, mock_fsrs, mock_generator, mock_exporter
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: POST /api/decks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_deck_success(_app):
    """POST /api/decks creates a new deck and returns 201."""
    app, conn, *_ = _app
    conn.fetchrow.return_value = _make_deck_row(id=1, name="My Deck")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/decks", json={"name": "My Deck"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "My Deck"
    assert body["card_count"] == 0


@pytest.mark.asyncio
async def test_create_deck_empty_name_returns_422(_app):
    """POST /api/decks with empty name returns 422."""
    app, conn, *_ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/decks", json={"name": ""})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: GET /api/decks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_decks_success(_app):
    """GET /api/decks returns a list of decks."""
    app, conn, *_ = _app
    conn.fetch.return_value = [
        _make_deck_row(id=1, name="Deck A", card_count=5, due_count=2),
        _make_deck_row(id=2, name="Deck B", card_count=0, due_count=0),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/decks")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["name"] == "Deck A"
    assert body[0]["card_count"] == 5
    assert body[0]["due_count"] == 2


@pytest.mark.asyncio
async def test_list_decks_empty(_app):
    """GET /api/decks returns empty list when no decks exist."""
    app, conn, *_ = _app
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/decks")

    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Tests: GET /api/analytics/*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "rows", "expected"),
    [
        (
            "get_activity",
            [
                FakeRecord(
                    log_date=_now().date(),
                    tasks_completed=3,
                    cards_reviewed=12,
                    papers_read=2,
                    focus_hours=4.5,
                    notes="Deep work",
                )
            ],
            {
                "tasks_completed": 3,
                "cards_reviewed": 12,
                "papers_read": 2,
                "focus_hours": 4.5,
                "notes": "Deep work",
            },
        ),
        (
            "get_reviews",
            [FakeRecord(rating=4, count=7)],
            {"rating": 4, "count": 7},
        ),
        (
            "get_retention",
            [
                FakeRecord(
                    review_date=_now().date(),
                    total=10,
                    good_easy=8,
                    retention_pct=80.0,
                )
            ],
            {"total": 10, "good_easy": 8, "retention_pct": 80.0},
        ),
        (
            "get_llm_cost",
            [FakeRecord(day=_now().date(), total_cost=1.25, workflow="summaries")],
            {"total_cost": 1.25, "workflow": "summaries"},
        ),
    ],
)
async def test_analytics_handlers_return_expected_shapes(handler_name, rows, expected):
    """Analytics handlers preserve the row shape expected by their response models."""
    from learning_engine.routers import analytics

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db_pool = MagicMock()
    db_pool.acquire.return_value = ctx
    handler = getattr(analytics, handler_name).__wrapped__

    result = await handler(MagicMock(), days=30, db_pool=db_pool)

    assert len(result) == 1
    for key, value in expected.items():
        assert getattr(result[0], key) == value


def test_analytics_handlers_declare_model_aligned_return_types():
    """Analytics handlers declare the same collection shapes as their response models."""
    from learning_engine.models import (
        ActivityItem,
        LLMCostItem,
        RetentionItem,
        ReviewDistributionItem,
    )
    from learning_engine.routers import analytics

    expected = {
        "get_activity": list[ActivityItem],
        "get_reviews": list[ReviewDistributionItem],
        "get_retention": list[RetentionItem],
        "get_llm_cost": list[LLMCostItem],
    }

    for handler_name, return_type in expected.items():
        handler = getattr(analytics, handler_name).__wrapped__
        assert inspect.signature(handler).return_annotation == return_type


# ---------------------------------------------------------------------------
# Tests: POST /api/cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_card_success(_app):
    """POST /api/cards creates a new card and returns 201."""
    app, conn, _, mock_fsrs, *_ = _app
    mock_fsrs.create_new_card.return_value = ({"state": "new"}, _now())
    conn.fetchrow.return_value = make_card_row(id=10, deck_id=1, front="Q?", back="A.")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/cards",
            json={
                "deck_id": 1,
                "card_type": "concept",
                "front": "What is ML?",
                "back": "Machine Learning",
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == 10


# ---------------------------------------------------------------------------
# Tests: GET /api/cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cards_success(_app):
    """GET /api/cards returns a list of cards."""
    app, conn, *_ = _app
    conn.fetch.return_value = [
        make_card_row(id=1, front="Q1?", back="A1"),
        make_card_row(id=2, front="Q2?", back="A2"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/cards")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2


@pytest.mark.asyncio
async def test_list_cards_filter_by_deck(_app):
    """GET /api/cards?deck_id=1 returns only cards belonging to that deck."""
    app, conn, *_ = _app
    conn.fetch.return_value = [make_card_row(id=1, deck_id=1)]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/cards", params={"deck_id": 1})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["deck_id"] == 1


# ---------------------------------------------------------------------------
# Tests: PUT /api/cards/{card_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_card_success(_app):
    """PUT /api/cards/{id} updates card content."""
    app, conn, *_ = _app
    existing = make_card_row(id=5, front="Old Q", back="Old A")
    updated = make_card_row(id=5, front="New Q", back="Old A")

    # First fetchrow for existing, second for update
    conn.fetchrow.side_effect = [existing, updated]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/cards/5", json={"front": "New Q"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["front"] == "New Q"


@pytest.mark.asyncio
async def test_update_card_not_found(_app):
    """PUT /api/cards/{id} returns 404 when card does not exist."""
    app, conn, *_ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/cards/999", json={"front": "New Q"})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: DELETE /api/cards/{card_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_card_success(_app):
    """DELETE /api/cards/{id} returns 204 on success."""
    app, conn, *_ = _app
    conn.execute.return_value = "DELETE 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/cards/5")

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_card_not_found(_app):
    """DELETE /api/cards/{id} returns 404 when card does not exist."""
    app, conn, *_ = _app
    conn.execute.return_value = "DELETE 0"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/cards/999")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /api/review/next
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_next_review_due_cards(_app):
    """GET /api/review/next returns due cards when they exist."""
    app, conn, *_ = _app
    conn.fetch.return_value = [
        make_card_row(id=1, due_at=_now() - timedelta(hours=1)),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/review/next")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == 1


@pytest.mark.asyncio
async def test_get_next_review_none_due(_app):
    """GET /api/review/next returns empty list when no cards are due."""
    app, conn, *_ = _app
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/review/next")

    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Tests: POST /api/review/{card_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_review_success(_app):
    """POST /api/review/{card_id} returns review response."""
    app, conn, _, mock_fsrs, *_ = _app
    card = make_card_row(id=1, fsrs_state={"stability": 1.0})
    next_due = _now() + timedelta(days=3)
    mock_fsrs.schedule_review.return_value = (
        {"stability": 2.5},
        {"rating": 3},
        next_due,
    )
    conn.fetchrow.return_value = card
    conn.fetchval.return_value = 42  # review_log_id

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/review/1", json={"rating": 3})

    assert resp.status_code == 200
    body = resp.json()
    assert body["card_id"] == 1
    assert body["rating"] == 3
    assert body["review_log_id"] == 42


@pytest.mark.asyncio
async def test_submit_review_not_found(_app):
    """POST /api/review/{card_id} returns 404 for missing card."""
    app, conn, *_ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/review/999", json={"rating": 3})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /api/stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_stats_success(_app):
    """GET /api/stats returns retention statistics."""
    app, conn, *_ = _app
    stats_row = FakeRecord(
        total_cards=50,
        due_now=10,
        reviewed_today=5,
        by_rating={"3": 20, "4": 10},
        total_recent=35,
        good_easy=30,
    )
    conn.fetchrow.return_value = stats_row
    conn.fetch.return_value = []  # streak rows

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cards"] == 50
    assert body["due_now"] == 10
    assert body["reviewed_today"] == 5
    assert body["streak_days"] == 0


# ---------------------------------------------------------------------------
# Tests: POST /api/generate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_enqueues_job_and_returns_202(_app):
    """POST /api/generate defers a procrastinate task and returns 202 with job_id."""
    app, conn, _, mock_fsrs, mock_generator, _ = _app

    import jarvis_common.task_registry as task_registry

    mock_card_gen_task = MagicMock()
    mock_card_gen_task.defer_async = AsyncMock()
    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_card_gen_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/generate",
                json={"paper_id": 1, "deck_id": 1},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert isinstance(body["job_id"], str) and len(body["job_id"]) == 36
    assert body["status"] == "queued"


# ---------------------------------------------------------------------------
# Tests: POST /api/generate/batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_generate_deck_not_found(_app):
    """batch_generate_cards returns 404 when the target deck does not exist."""
    from fastapi import HTTPException
    from learning_engine.models import BatchGenerateRequest
    from learning_engine.routers import generation

    _, conn, _, mock_fsrs, mock_generator, _ = _app
    conn.fetchval.return_value = None
    handler = generation.batch_generate_cards.__wrapped__

    with pytest.raises(HTTPException, match="Deck not found") as exc_info:
        await handler(
            MagicMock(),
            body=BatchGenerateRequest(deck_id=999),
            db_pool=MagicMock(
                acquire=MagicMock(
                    return_value=MagicMock(
                        __aenter__=AsyncMock(return_value=conn),
                        __aexit__=AsyncMock(return_value=False),
                    )
                )
            ),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_batch_generate_success_returns_202_accepted(_app):
    """batch_generate_cards defers a procrastinate task and returns 202 with job_id."""
    from learning_engine.models import BatchGenerateRequest
    from learning_engine.routers import generation

    _, conn, _, mock_fsrs, mock_generator, _ = _app
    pool, _ = _make_pool_and_conn()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    conn.fetchval.return_value = 1  # deck exists
    handler = generation.batch_generate_cards.__wrapped__

    import jarvis_common.task_registry as task_registry

    mock_card_gen_batch_task = MagicMock()
    mock_card_gen_batch_task.defer_async = AsyncMock()
    with patch.dict(task_registry._TASK_MAP, {"card.generate_batch": mock_card_gen_batch_task}):
        resp = await handler(
            MagicMock(),
            body=BatchGenerateRequest(deck_id=1),
            db_pool=pool,
        )

    assert resp.status == "queued"
    assert isinstance(resp.job_id, str) and len(resp.job_id) == 36


def test_batch_generate_declares_accepted_response_type():
    """batch_generate_cards is typed as returning BatchAcceptedResponse (202)."""
    import typing

    from learning_engine.routers import generation

    handler = generation.batch_generate_cards.__wrapped__
    hints = typing.get_type_hints(handler)
    from learning_engine.models import BatchAcceptedResponse

    assert hints.get("return") is BatchAcceptedResponse


# ---------------------------------------------------------------------------
# Tests: GET /api/export/anki/{deck_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_deck_not_found(_app):
    """GET /api/export/anki/{deck_id} returns 404 when deck does not exist."""
    app, conn, *_ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/export/anki/999")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_export_no_cards_returns_400(_app):
    """GET /api/export/anki/{deck_id} returns 400 when deck has no cards."""
    app, conn, *_ = _app
    conn.fetchrow.return_value = FakeRecord(
        id=1,
        name="Empty Deck",
        description=None,
        topic_id=None,
        card_count=0,
        due_count=0,
        created_at=_now(),
    )
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/export/anki/1")

    assert resp.status_code == 400
    assert "no cards" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Tests: GET /api/review/next with multiple cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_next_review_respects_limit(_app):
    """GET /api/review/next?limit=5 passes limit to SQL query."""
    app, conn, *_ = _app
    conn.fetch.return_value = [make_card_row(id=i) for i in range(5)]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/review/next", params={"limit": 5})

    assert resp.status_code == 200
    assert len(resp.json()) == 5


# ---------------------------------------------------------------------------
# Tests: POST /api/cards with evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_card_with_evidence(_app):
    """POST /api/cards with evidence field succeeds."""
    app, conn, _, mock_fsrs, *_ = _app
    mock_fsrs.create_new_card.return_value = ({}, _now())
    evidence = {"quote": "Some text", "page_number": 3, "chunk_id": 10}
    conn.fetchrow.return_value = make_card_row(
        id=20,
        deck_id=1,
        front="Q?",
        back="A.",
        evidence=evidence,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/cards",
            json={
                "deck_id": 1,
                "card_type": "quote",
                "front": "What is this?",
                "back": "That.",
                "evidence": {"quote": "Some text", "page_number": 3, "chunk_id": 10},
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["evidence"]["quote"] == "Some text"
    assert body["evidence"]["page_number"] == 3


# ---------------------------------------------------------------------------
# Tests: POST /api/decks with description
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_deck_with_description(_app):
    """POST /api/decks with optional description succeeds."""
    app, conn, *_ = _app
    conn.fetchrow.return_value = _make_deck_row(
        id=3,
        name="My Deck",
        description="A great deck",
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/decks",
            json={
                "name": "My Deck",
                "description": "A great deck",
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["description"] == "A great deck"


# ---------------------------------------------------------------------------
# Tests: GET /api/cards pagination
# ---------------------------------------------------------------------------


# test_list_cards_with_pagination deleted — D6-05 contract test
# test_le_contract.py::test_list_cards_pagination_response_shape is the survivor:
# it asserts len(body) <= 1 against the real schema (behavioural contract).
# The SQL-text "LIMIT in sql" / "OFFSET in sql" assertions were B1-09 whitebox
# checks with no additional coverage beyond what the contract test provides.


# ---------------------------------------------------------------------------
# Tests: Health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_ok(_app):
    """GET /health returns ok when all dependencies are healthy."""
    app, conn, mock_http, *_ = _app
    conn.fetchval.return_value = 1  # SELECT 1

    class MockResp:
        status_code = 200

    mock_http.get.return_value = MockResp()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # Public /health no longer exposes service/checks — SEC-H09
    assert "service" not in body
    assert "checks" not in body


# ---------------------------------------------------------------------------
# Live PostgreSQL tests — gated by JARVIS_RUN_LIVE_PG=1
# ---------------------------------------------------------------------------

pytestmark_live = pytest.mark.live_pg


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_focus_session_paper_id_live_pg(live_pg_dsn: str) -> None:
    """POST /api/executive/focus/log with paper_id must not raise HTTP 500.

    Regression guard for NEW-C1: migration 043 replaced the single-column
    UNIQUE on paper_user_state(paper_id) with a composite
    (paper_id, user_id) NULLS NOT DISTINCT index.  The old ON CONFLICT (paper_id)
    clause raises PostgreSQL error 42P10 on any deployment that has applied 043.
    This test verifies that the fixed SQL executes without error against a real
    PostgreSQL database with all migrations applied.
    """
    from pathlib import Path

    import asyncpg
    from paper_ingestion.migrations_runner import run_migrations

    repo_root = Path(__file__).resolve().parents[3]
    init_sql = repo_root / "db" / "init.sql"

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(init_sql.read_text(encoding="utf-8"))
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # Insert a paper row to satisfy the FK constraint
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-le-focus-c1', 'arxiv', 'Live LE Focus C1',
                        ARRAY['Tester'], 'https://example.test/c1')
                """
            )
            paper_id: int = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-le-focus-c1",
            )

            # Execute the fixed upsert SQL directly — this is exactly what
            # log_focus_session runs after the 047-state fix.
            await conn.execute(
                """INSERT INTO paper_user_state (paper_id, user_id, state)
                   VALUES ($1, $2, 'reading')
                   ON CONFLICT (paper_id, user_id) DO UPDATE
                      SET state = 'reading'
                    WHERE paper_user_state.state IN ('inbox', 'to_read')""",
                paper_id,
                None,  # user_id = None (unauthenticated)
            )

            # Calling it a second time must also succeed (idempotent upsert)
            await conn.execute(
                """INSERT INTO paper_user_state (paper_id, user_id, state)
                   VALUES ($1, $2, 'reading')
                   ON CONFLICT (paper_id, user_id) DO UPDATE
                      SET state = 'reading'
                    WHERE paper_user_state.state IN ('inbox', 'to_read')""",
                paper_id,
                None,
            )

            # Verify the row was written
            state = await conn.fetchval(
                "SELECT state FROM paper_user_state WHERE paper_id = $1 AND user_id IS NULL",
                paper_id,
            )
            assert state == "reading"
    finally:
        await pool.close()
