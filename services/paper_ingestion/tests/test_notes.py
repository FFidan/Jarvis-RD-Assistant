"""Tests for paper notes CRUD endpoints and models."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport
from paper_ingestion.models import NoteCreate, NoteResponse, NoteUpdate

from tests.conftest import _make_pool_and_conn

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


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked state for testing notes endpoints."""
    # Defer import so env vars / mocks apply

    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import notes as notes_router

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    # Disable rate limiting for tests
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # WS-CROSS-USER: the resolver now always yields a real user, so
    # assert_paper_ownership runs (it previously short-circuited on the
    # None caller). These notes tests exercise CRUD/promote behaviour, not
    # ownership; default it to a pass-through. Ownership-rejection tests
    # re-patch ``notes_router.assert_paper_ownership`` themselves.
    _orig_ownership = notes_router.assert_paper_ownership
    notes_router.assert_paper_ownership = AsyncMock(return_value=None)
    yield app, conn
    notes_router.assert_paper_ownership = _orig_ownership
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


# Collapsed (E2.PI): test_create_note_returns_201
# Survivor: test_notes_contract.py::test_notes_create_owner_gets_201
# POST /api/papers/{id}/notes creates note and returns 201 with body — verified with real DB.


# Collapsed (E2.PI): test_list_notes_returns_created
# Survivor: test_notes_contract.py::test_notes_list_owner_gets_own_note
# GET /api/papers/{id}/notes returns the owner's notes — verified with real DB.


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
    fetch_sql = conn.fetch.await_args.args[0]
    assert "source = $2" in fetch_sql
    assert "IS NOT DISTINCT FROM" not in fetch_sql
    assert "user_id = $3" in fetch_sql
    # args: paper_id, source, user_id (now 3 params)
    assert conn.fetch.await_args.args[1] == 1
    assert conn.fetch.await_args.args[2] == "zotero"


# Collapsed (E2.PI): test_update_note
# Survivor: test_notes_contract.py::test_a62_update_note_owner_gets_200
# PUT /api/notes/{id} updates note content for owner, returns 200 — verified with real DB.

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).


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


# ---------------------------------------------------------------------------
# PI-EDGE-004 — promote endpoint hardening tests
# ---------------------------------------------------------------------------

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).


async def test_promote_zero_chunks_marks_failed(_app):
    """POST /api/notes/{id}/promote when paper has no chunks records failed status."""
    app, conn = _app
    # Note with a highlight, but no matching chunks in the paper.
    conn.fetchrow.side_effect = [
        _make_note_record(
            note_id=8,
            source="zotero",
            highlight_text="Quote that cannot be found in chunks.",
        ),
        # Second fetchrow is the RETURNING * from the failed UPDATE.
        _make_note_record(
            note_id=8,
            source="zotero",
            highlight_text="Quote that cannot be found in chunks.",
            verification_status="failed",
        ),
    ]
    # No chunks for this paper.
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/8/promote")

    # Verification fails gracefully: 200 with failed status (not an exception).
    assert resp.status_code == 200
    assert resp.json()["verification_status"] == "failed"
    assert resp.json()["promoted_at"] is None


# ---------------------------------------------------------------------------
# WS-4 — page-window optimisation for promote_zotero_note
# ---------------------------------------------------------------------------


async def test_promote_page_window_optimization_used(_app):
    """WS-4: when note has page_number, first chunk fetch uses BETWEEN page-2 AND page+2."""
    app, conn = _app
    promoted_at = datetime(2026, 1, 4, tzinfo=UTC)

    # Note on page 5 with a highlight that exists in the window.
    note_record = _make_note_record(
        note_id=10,
        source="zotero",
        highlight_text="Neural networks learn hierarchical representations.",
        page_number=5,
    )
    conn.fetchrow.side_effect = [
        note_record,
        # UPDATE RETURNING * (verified)
        _make_note_record(
            note_id=10,
            source="zotero",
            highlight_text="Neural networks learn hierarchical representations.",
            page_number=5,
            verification_status="verified",
            verified_quote="Neural networks learn hierarchical representations.",
            verified_page_number=5,
            promoted_at=promoted_at,
        ),
    ]

    # Window fetch returns a matching chunk (page 5 is in the [3,7] window).
    window_chunk = {
        "id": 200,
        "paper_id": 1,
        "chunk_index": 4,
        "content": "Neural networks learn hierarchical representations.",
        "page_number": 5,
        "start_char": None,
        "end_char": None,
        "embedding_id": None,
        "created_at": _NOW,
    }
    # Return the window result on the first fetch call.
    conn.fetch.return_value = [window_chunk]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/10/promote")

    assert resp.status_code == 200
    assert resp.json()["verification_status"] == "verified"

    # The first conn.fetch call must be the page-window query.
    assert conn.fetch.await_count >= 1, "At least one chunk fetch must occur"
    first_fetch_args = conn.fetch.await_args_list[0].args
    first_sql = first_fetch_args[0]
    assert "BETWEEN" in first_sql, (
        "WS-4: first fetch must use BETWEEN page-window SQL, got: " + first_sql
    )
    # Parameters: paper_id, page-2=3, page+2=7
    assert first_fetch_args[2] == 3, f"Expected page-window low=3, got {first_fetch_args[2]}"
    assert first_fetch_args[3] == 7, f"Expected page-window high=7, got {first_fetch_args[3]}"


async def test_promote_falls_back_to_all_chunks_when_window_misses(_app):
    """WS-4: when the page-window fetch finds nothing, a second all-chunks fetch is issued."""
    app, conn = _app

    # Note on page 5 but the matching chunk is on page 99 (outside the window).
    note_record = _make_note_record(
        note_id=11,
        source="zotero",
        highlight_text="This sentence only appears on page ninety nine.",
        page_number=5,
    )
    conn.fetchrow.side_effect = [
        note_record,
        # UPDATE RETURNING * (failed — verifier can't find it in paper text either)
        _make_note_record(
            note_id=11,
            source="zotero",
            highlight_text="This sentence only appears on page ninety nine.",
            page_number=5,
            verification_status="failed",
        ),
    ]

    far_chunk = {
        "id": 300,
        "paper_id": 1,
        "chunk_index": 98,
        "content": "This sentence only appears on page ninety nine.",
        "page_number": 99,
        "start_char": None,
        "end_char": None,
        "embedding_id": None,
        "created_at": _NOW,
    }

    # First fetch (page window [3,7]) returns empty — no chunks in that range.
    # Second fetch (all chunks) returns the far chunk.
    conn.fetch.side_effect = [
        [],  # window miss
        [far_chunk],  # full-paper fallback
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/11/promote")

    assert resp.status_code == 200
    # Two fetch calls: one window (empty), one full fallback.
    assert conn.fetch.await_count == 2, (
        f"Expected 2 fetch calls (window + fallback), got {conn.fetch.await_count}"
    )
    second_fetch_args = conn.fetch.await_args_list[1].args
    second_sql = second_fetch_args[0]
    # The fallback SQL must NOT contain BETWEEN — it is the unrestricted query.
    assert "BETWEEN" not in second_sql, (
        "WS-4 fallback: second fetch must be full-paper query (no BETWEEN)"
    )


# ---------------------------------------------------------------------------
# WS-6B-α — multi-user ownership wiring on /api/notes endpoints.
# ---------------------------------------------------------------------------


async def _async_user_99(_request, *_args, **_kwargs):
    return 99


# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).

# Cluster 3 deletion (2026-05-22): superseded by test_pi_notes_promote_contract.py (N-01..N-06).
