"""Tests for paper notes CRUD endpoints and models."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
        source="user",
        zotero_annotation_key=None,
        created_at=now,
    )
    assert resp.id == 1
    assert resp.paper_id == 42
    assert resp.page_number == 3
    assert resp.source == "user"


def test_note_response_accepts_zotero_source():
    """Imported Zotero annotations are represented as read-only note rows."""
    now = datetime.now(tz=UTC)
    resp = NoteResponse(
        id=2,
        paper_id=42,
        user_note="Comment from Zotero",
        highlight_text="Highlighted text",
        page_number=7,
        source="zotero",
        zotero_annotation_key="ABCD1234",
        created_at=now,
    )
    assert resp.source == "zotero"
    assert resp.zotero_annotation_key == "ABCD1234"
    assert resp.verification_status == "unverified"


def test_note_response_accepts_verified_promotion_fields():
    """Promoted Zotero annotations carry explicit verification metadata."""
    now = datetime.now(tz=UTC)
    resp = NoteResponse(
        id=2,
        paper_id=42,
        user_note="Comment from Zotero",
        highlight_text="Highlighted text",
        page_number=7,
        source="zotero",
        zotero_annotation_key="ABCD1234",
        verification_status="verified",
        verified_quote="Highlighted text",
        verified_page_number=7,
        promoted_at=now,
        created_at=now,
    )
    assert resp.verification_status == "verified"
    assert resp.promoted_at == now


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
    source: str = "user",
    zotero_annotation_key: str | None = None,
    verification_status: str = "unverified",
    verified_quote: str | None = None,
    verified_page_number: int | None = None,
    promoted_at: datetime | None = None,
) -> dict:
    """Build a dict mimicking an asyncpg.Record for paper_notes."""
    return {
        "id": note_id,
        "paper_id": paper_id,
        "user_note": user_note,
        "highlight_text": highlight_text,
        "page_number": page_number,
        "source": source,
        "zotero_annotation_key": zotero_annotation_key,
        "verification_status": verification_status,
        "verified_quote": verified_quote,
        "verified_page_number": verified_page_number,
        "promoted_at": promoted_at,
        "created_at": _NOW,
    }


def _mock_pool() -> tuple[MagicMock, AsyncMock]:
    """Create a mock asyncpg pool with context-managed connection."""
    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked state for testing notes endpoints."""
    # Defer import so env vars / mocks apply
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _mock_pool()
    app.state.db_pool = pool
    # Disable rate limiting for tests
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


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


async def test_list_notes_filters_by_source(_app):
    """GET /api/papers/{id}/notes?source=zotero filters imported annotations."""
    app, conn = _app
    conn.fetch.return_value = [
        _make_note_record(note_id=2, user_note="highlight", source="zotero"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/1/notes?source=zotero")

    assert resp.status_code == 200
    assert resp.json()[0]["source"] == "zotero"
    assert "source = $2" in conn.fetch.await_args.args[0]
    assert conn.fetch.await_args.args[1:] == (1, "zotero")


async def test_update_note(_app):
    """PUT /api/notes/{id} updates note text."""
    app, conn = _app

    with patch(
        "paper_ingestion.routers.notes.dynamic_update",
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


async def test_update_zotero_note_is_rejected(_app):
    """Imported Zotero annotations are read-only through the notes API."""
    app, conn = _app
    conn.fetchval.return_value = "zotero"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/notes/1", json={"user_note": "updated"})

    assert resp.status_code == 403


async def test_delete_note_returns_204(_app):
    """DELETE /api/notes/{id} returns 204 on success."""
    app, conn = _app

    with patch("paper_ingestion.routers.notes.delete_or_404", new_callable=AsyncMock):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/notes/1")

    assert resp.status_code == 204


async def test_delete_zotero_note_is_rejected(_app):
    """Imported Zotero annotations cannot be deleted via the user-note endpoint."""
    app, conn = _app
    conn.fetchval.return_value = "zotero"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/notes/1")

    assert resp.status_code == 403


async def test_promote_zotero_note_verifies_highlight(_app):
    """POST /api/notes/{id}/promote marks a verified Zotero highlight as promoted."""
    app, conn = _app
    promoted_at = datetime(2026, 1, 2, tzinfo=UTC)
    conn.fetchrow.side_effect = [
        _make_note_record(
            note_id=5,
            source="zotero",
            zotero_annotation_key="Z1",
            highlight_text="The method improves accuracy.",
            page_number=2,
        ),
        _make_note_record(
            note_id=5,
            source="zotero",
            zotero_annotation_key="Z1",
            highlight_text="The method improves accuracy.",
            page_number=2,
            verification_status="verified",
            verified_quote="The method improves accuracy.",
            verified_page_number=2,
            promoted_at=promoted_at,
        ),
    ]
    conn.fetch.return_value = [
        {
            "id": 100,
            "paper_id": 1,
            "chunk_index": 0,
            "content": "The method improves accuracy.",
            "page_number": 2,
            "start_char": None,
            "end_char": None,
            "embedding_id": None,
            "created_at": _NOW,
        }
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/5/promote")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verification_status"] == "verified"
    assert body["verified_page_number"] == 2
    assert body["promoted_at"] is not None


async def test_promote_rejects_non_zotero_note(_app):
    """Only Zotero annotation notes can use the promotion endpoint."""
    app, conn = _app
    conn.fetchrow.return_value = _make_note_record(source="user", highlight_text="quote")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/1/promote")

    assert resp.status_code == 400


async def test_promote_rejects_zotero_note_without_highlight(_app):
    """Promotion requires highlight text, not just a free-form Zotero comment."""
    app, conn = _app
    conn.fetchrow.return_value = _make_note_record(source="zotero", highlight_text=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/1/promote")

    assert resp.status_code == 400


async def test_promote_marks_failed_when_highlight_does_not_verify(_app):
    """Failed quote verification is recorded without promoting the note."""
    app, conn = _app
    conn.fetchrow.side_effect = [
        _make_note_record(source="zotero", highlight_text="Invented quote"),
        _make_note_record(
            source="zotero",
            highlight_text="Invented quote",
            verification_status="failed",
        ),
    ]
    conn.fetch.return_value = [
        {
            "id": 100,
            "paper_id": 1,
            "chunk_index": 0,
            "content": "Actual paper text.",
            "page_number": 2,
            "start_char": None,
            "end_char": None,
            "embedding_id": None,
            "created_at": _NOW,
        }
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/1/promote")

    assert resp.status_code == 200
    assert resp.json()["verification_status"] == "failed"
    assert resp.json()["promoted_at"] is None


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

    with patch("paper_ingestion.routers.notes.delete_or_404", side_effect=_raise_404):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/notes/999")

    assert resp.status_code == 404
