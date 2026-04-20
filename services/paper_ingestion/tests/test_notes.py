"""Tests for paper notes CRUD endpoints and models."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport
from paper_ingestion.models import NoteCreate, NoteResponse, NoteUpdate

# ---------------------------------------------------------------------------
# Pydantic model tests
# ---------------------------------------------------------------------------


def test_note_create_valid():
    """NoteCreate accepts valid note text with optional fields."""
    note = NoteCreate(user_note="Important finding", page_number=5)
    assert note.user_note == "Important finding"
    assert note.page_number == 5
    assert note.highlight_text is None


def test_note_create_rejects_empty():
    """NoteCreate rejects empty user_note."""
    with pytest.raises(Exception):
        NoteCreate(user_note="")


def test_note_create_rejects_invalid_page():
    """NoteCreate rejects page_number < 1."""
    with pytest.raises(Exception):
        NoteCreate(user_note="valid note", page_number=0)


def test_note_update_partial():
    """NoteUpdate allows partial updates via exclude_unset."""
    body = NoteUpdate(user_note="updated text")
    dump = body.model_dump(exclude_unset=True)
    assert dump == {"user_note": "updated text"}
    assert "highlight_text" not in dump
    assert "page_number" not in dump


def test_note_response_from_dict():
    """NoteResponse parses a typical database row dict."""
    now = datetime.now(tz=UTC)
    resp = NoteResponse(
        id=1,
        paper_id=42,
        user_note="test",
        highlight_text=None,
        page_number=3,
        created_at=now,
    )
    assert resp.id == 1
    assert resp.paper_id == 42
    assert resp.page_number == 3


# ---------------------------------------------------------------------------
# Endpoint integration tests (mocked DB)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_note_record(
    note_id: int = 1,
    paper_id: int = 1,
    user_note: str = "test note",
    highlight_text: str | None = None,
    page_number: int | None = None,
) -> dict:
    """Build a dict mimicking an asyncpg.Record for paper_notes."""
    return {
        "id": note_id,
        "paper_id": paper_id,
        "user_note": user_note,
        "highlight_text": highlight_text,
        "page_number": page_number,
        "created_at": _NOW,
    }


def _mock_pool() -> AsyncMock:
    """Create a mock asyncpg pool with context-managed connection."""
    pool = AsyncMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked state for testing notes endpoints."""
    # Defer import so env vars / mocks apply
    from paper_ingestion.main import app

    pool, conn = _mock_pool()
    app.state.db_pool = pool
    # Disable rate limiting for tests
    app.state.limiter.enabled = False
    return app, conn


async def test_list_notes_empty(_app):
    """GET /api/papers/{id}/notes returns empty list when no notes exist."""
    app, conn = _app
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/1/notes")

    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_note_returns_201(_app):
    """POST /api/papers/{id}/notes creates and returns note with 201."""
    app, conn = _app
    conn.fetchrow.side_effect = [
        {"id": 1},  # paper exists check
        _make_note_record(user_note="my note", page_number=3),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/papers/1/notes",
            json={"user_note": "my note", "page_number": 3},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["user_note"] == "my note"
    assert body["page_number"] == 3
    assert body["paper_id"] == 1


async def test_list_notes_returns_created(_app):
    """GET /api/papers/{id}/notes returns notes that exist."""
    app, conn = _app
    conn.fetch.return_value = [
        _make_note_record(note_id=1, user_note="first"),
        _make_note_record(note_id=2, user_note="second"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/1/notes")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["user_note"] == "first"


async def test_update_note(_app):
    """PUT /api/notes/{id} updates note text."""
    app, conn = _app

    with patch(
        "paper_ingestion.main.dynamic_update",
        new_callable=AsyncMock,
        return_value=_make_note_record(user_note="updated"),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/notes/1",
                json={"user_note": "updated"},
            )

    assert resp.status_code == 200
    assert resp.json()["user_note"] == "updated"


async def test_delete_note_returns_204(_app):
    """DELETE /api/notes/{id} returns 204 on success."""
    app, conn = _app

    with patch("paper_ingestion.main.delete_or_404", new_callable=AsyncMock):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/notes/1")

    assert resp.status_code == 204


async def test_create_note_paper_not_found(_app):
    """POST /api/papers/{id}/notes returns 404 for nonexistent paper."""
    app, conn = _app
    conn.fetchrow.return_value = None  # paper does not exist

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/papers/999/notes",
            json={"user_note": "orphan note"},
        )

    assert resp.status_code == 404


async def test_delete_note_not_found(_app):
    """DELETE /api/notes/{id} returns 404 when note does not exist."""
    from fastapi import HTTPException

    app, conn = _app

    async def _raise_404(*args, **kwargs):
        raise HTTPException(status_code=404, detail="Not found")

    with patch("paper_ingestion.main.delete_or_404", side_effect=_raise_404):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/notes/999")

    assert resp.status_code == 404
