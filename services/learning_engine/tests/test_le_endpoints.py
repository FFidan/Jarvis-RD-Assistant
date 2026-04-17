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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Dict subclass that supports both dict[key] and .keys() like asyncpg.Record."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def keys(self):
        return super().keys()

    def get(self, key, default=None):
        return super().get(key, default)

    def values(self):
        return super().values()


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async CM."""
    conn = AsyncMock()

    # Make conn.transaction() return a proper async context manager (not a coroutine)
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _now():
    return datetime.now(UTC)


def _make_deck_row(
    id=1, name="Test Deck", description=None, topic_id=None, card_count=0, due_count=0
):
    return FakeRecord(
        id=id,
        name=name,
        description=description,
        topic_id=topic_id,
        card_count=card_count,
        due_count=due_count,
        created_at=_now(),
    )


def _make_card_row(
    id=1,
    deck_id=1,
    paper_id=None,
    card_type="concept",
    front="Q?",
    back="A.",
    evidence=None,
    fsrs_state=None,
    due_at=None,
):
    return FakeRecord(
        id=id,
        deck_id=deck_id,
        paper_id=paper_id,
        card_type=card_type,
        front=front,
        back=back,
        evidence=evidence or {},
        fsrs_state=fsrs_state or {},
        due_at=due_at or _now(),
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked dependencies and disabled auth."""
    from app.deps import get_anki_exporter, get_card_generator, get_db_pool, get_fsrs_manager
    from app.main import app
    from jarvis_common import verify_api_key

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

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_card_generator] = lambda: mock_generator
    app.dependency_overrides[get_anki_exporter] = lambda: mock_exporter

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
    from app.routers import analytics

    db_pool = AsyncMock()
    db_pool.fetch.return_value = rows
    handler = getattr(analytics, handler_name).__wrapped__

    result = await handler(MagicMock(), days=30, db_pool=db_pool)

    assert len(result) == 1
    for key, value in expected.items():
        assert result[0][key] == value


def test_analytics_handlers_declare_model_aligned_return_types():
    """Analytics handlers declare the same collection shapes as their response models."""
    from app.models import ActivityItem, LLMCostItem, RetentionItem, ReviewDistributionItem
    from app.routers import analytics

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
    conn.fetchrow.return_value = _make_card_row(id=10, deck_id=1, front="Q?", back="A.")

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
        _make_card_row(id=1, front="Q1?", back="A1"),
        _make_card_row(id=2, front="Q2?", back="A2"),
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
    """GET /api/cards?deck_id=1 passes deck filter to SQL."""
    app, conn, *_ = _app
    conn.fetch.return_value = [_make_card_row(id=1, deck_id=1)]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/cards", params={"deck_id": 1})

    assert resp.status_code == 200
    # Verify SQL contains deck_id filter
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "deck_id" in sql


# ---------------------------------------------------------------------------
# Tests: PUT /api/cards/{card_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_card_success(_app):
    """PUT /api/cards/{id} updates card content."""
    app, conn, *_ = _app
    existing = _make_card_row(id=5, front="Old Q", back="Old A")
    updated = _make_card_row(id=5, front="New Q", back="Old A")

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
        _make_card_row(id=1, due_at=_now() - timedelta(hours=1)),
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
    card = _make_card_row(id=1, fsrs_state={"stability": 1.0})
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
    """POST /api/generate now enqueues a DB job and returns 202 with job_id."""
    from app.routers import generation

    app, conn, _, mock_fsrs, mock_generator, _ = _app

    fake_job_id = "cccccccc-dddd-eeee-ffff-000000000001"

    with patch.object(generation.jobs_lib, "enqueue", AsyncMock(return_value=fake_job_id)):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/generate",
                json={"paper_id": 1, "deck_id": 1},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == fake_job_id
    assert body["status"] == "queued"


# ---------------------------------------------------------------------------
# Tests: POST /api/generate/batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_generate_deck_not_found(_app):
    """batch_generate_cards returns 404 when the target deck does not exist."""
    from app.models import BatchGenerateRequest
    from app.routers import generation
    from fastapi import HTTPException

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
    """batch_generate_cards enqueues a DB job and returns 202 with job_id."""
    from app.models import BatchGenerateRequest
    from app.routers import generation

    _, conn, _, mock_fsrs, mock_generator, _ = _app
    pool, _ = _make_pool_and_conn()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    conn.fetchval.return_value = 1  # deck exists
    fake_job_id = "dddddddd-eeee-ffff-0000-111111111111"
    handler = generation.batch_generate_cards.__wrapped__

    with patch.object(generation.jobs_lib, "enqueue", AsyncMock(return_value=fake_job_id)):
        resp = await handler(
            MagicMock(),
            body=BatchGenerateRequest(deck_id=1),
            db_pool=pool,
        )

    assert resp.status == "queued"
    assert resp.job_id == fake_job_id


def test_batch_generate_declares_accepted_response_type():
    """batch_generate_cards is typed as returning BatchAcceptedResponse (202)."""
    import typing

    from app.routers import generation

    handler = generation.batch_generate_cards.__wrapped__
    hints = typing.get_type_hints(handler)
    from app.models import BatchAcceptedResponse

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
    conn.fetch.return_value = [_make_card_row(id=i) for i in range(5)]

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
    conn.fetchrow.return_value = _make_card_row(
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


@pytest.mark.asyncio
async def test_list_cards_with_pagination(_app):
    """GET /api/cards with limit and offset returns correct subset."""
    app, conn, *_ = _app
    conn.fetch.return_value = [_make_card_row(id=3)]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/cards", params={"limit": 1, "offset": 2})

    assert resp.status_code == 200
    # Verify LIMIT and OFFSET are in the SQL
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "LIMIT" in sql
    assert "OFFSET" in sql


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
    assert body["service"] == "learning_engine"
