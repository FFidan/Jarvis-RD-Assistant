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
    from unittest.mock import AsyncMock

    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import notes as notes_router

    pool, conn = _mock_pool()
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
    fetch_sql = conn.fetch.await_args.args[0]
    assert "source = $2" in fetch_sql
    assert "IS NOT DISTINCT FROM" not in fetch_sql
    assert "user_id = $3" in fetch_sql
    # args: paper_id, source, user_id (now 3 params)
    assert conn.fetch.await_args.args[1] == 1
    assert conn.fetch.await_args.args[2] == "zotero"


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


# ---------------------------------------------------------------------------
# PI-EDGE-004 — promote endpoint hardening tests
# ---------------------------------------------------------------------------


async def test_promote_returns_404_for_missing_note_id(_app):
    """POST /api/notes/{id}/promote returns 404 when note does not exist."""
    app, conn = _app
    conn.fetchrow.return_value = None  # note not found

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/99999/promote")

    assert resp.status_code == 404
    assert "99999" in resp.json()["detail"]


async def test_promote_already_verified_is_idempotent(_app):
    """POST /api/notes/{id}/promote on an already-verified note returns 200 without re-verifying."""
    app, conn = _app
    promoted_at = datetime(2026, 1, 3, tzinfo=UTC)
    already_verified = _make_note_record(
        note_id=7,
        source="zotero",
        highlight_text="Some verified quote.",
        verification_status="verified",
        verified_quote="Some verified quote.",
        verified_page_number=3,
        promoted_at=promoted_at,
    )
    # fetchrow returns the already-verified note; no UPDATE should be issued.
    conn.fetchrow.return_value = already_verified

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/7/promote")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verification_status"] == "verified"
    assert body["promoted_at"] is not None
    # No UPDATE — conn.fetchrow should be called exactly once (the initial SELECT),
    # and conn.execute / the second fetchrow for UPDATE should NOT be called.
    assert conn.fetchrow.await_count == 1, (
        "Idempotency guard should have short-circuited before the UPDATE fetchrow"
    )


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


async def test_promote_note_403_for_other_user(_app, monkeypatch):
    """Sprint B: promote endpoint enforces canonical-corpus ownership.

    Paper discovered_by=42 + caller user_id=99 + caller's user_library does
    not contain paper → 403. The helper reads ``discovered_by`` (with legacy
    ``user_id`` fallback) then probes ``user_library`` via fetchval.
    """
    from jarvis_common import assert_paper_ownership as _real_assert_ownership

    monkeypatch.setattr(
        "paper_ingestion.routers.notes.current_user_id_strict_with_owner_override", _async_user_99
    )
    app, conn = _app
    # _app pass-throughs ownership by default; this test exercises the real
    # canonical-corpus ownership guard, so restore it.
    monkeypatch.setattr(
        "paper_ingestion.routers.notes.assert_paper_ownership", _real_assert_ownership
    )
    # First fetchrow = SELECT * FROM paper_notes WHERE id=$1 (zotero source).
    # Second fetchrow = ownership SELECT discovered_by FROM papers (mock still
    # ships ``user_id`` for backward-compat — the helper falls back to it).
    conn.fetchrow.side_effect = [
        _make_note_record(
            note_id=5,
            paper_id=99,
            source="zotero",
            zotero_annotation_key="Z1",
            highlight_text="quote",
        ),
        {"user_id": 42},  # discoverer = 42 (legacy key, helper handles)
    ]
    # Sprint B: force the user_library probe to MISS so the 403 fires.
    conn.fetchval = AsyncMock(return_value=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/5/promote")

    assert resp.status_code == 403
    assert "not owned" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# DOM-A-05 / DOM-A-06 / DOM-A-14 — note-authorship scoping
# ---------------------------------------------------------------------------


async def _async_user_7(_request, *_args, **_kwargs):
    return 7


async def _async_user_8(_request, *_args, **_kwargs):
    return 8


async def test_list_notes_scopes_to_author(_app, monkeypatch):
    """DOM-A-14: list_notes scopes to the caller's user_id.

    User 7 creates 2 notes (user_id=7).  User 8 calls list_notes — the SQL
    includes an exact ``user_id = $2`` scope so the mock returns an empty
    list because the conn.fetch stub returns [].
    """
    monkeypatch.setattr(
        "paper_ingestion.routers.notes.current_user_id_strict_with_owner_override", _async_user_8
    )
    app, conn = _app
    # assert_paper_ownership: single fetchrow for paper lookup (user_id=8 → None discovery → allow)
    conn.fetchrow.return_value = {"discovered_by": None}
    # The user-scoped SELECT returns empty (user 7's notes, not user 8's)
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/1/notes")

    assert resp.status_code == 200
    assert resp.json() == []
    # Confirm the SQL carries an exact user_id scoping parameter
    fetch_sql = conn.fetch.await_args.args[0]
    assert "IS NOT DISTINCT FROM" not in fetch_sql
    assert "user_id = $2" in fetch_sql


async def test_update_note_rejects_non_author(_app, monkeypatch):
    """DOM-A-05: update_note returns 404 when caller is not the note author.

    Note was created by user 7. User 8 attempts to update it.  The authorship
    SELECT (id + exact user_id match) returns None → 404.
    """
    monkeypatch.setattr(
        "paper_ingestion.routers.notes.current_user_id_strict_with_owner_override", _async_user_8
    )
    app, conn = _app
    # First fetchval: source check → "user" (not zotero)
    conn.fetchval.return_value = "user"
    # The authorship SELECT returns None (note belongs to user 7, not 8)
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/notes/1", json={"user_note": "hijack"})

    assert resp.status_code == 404


async def test_delete_note_rejects_non_author(_app, monkeypatch):
    """DOM-A-06: delete_note returns 404 when caller is not the note author.

    Note was created by user 7. User 8 attempts to delete it.  The authorship
    SELECT returns None → 404.
    """
    monkeypatch.setattr(
        "paper_ingestion.routers.notes.current_user_id_strict_with_owner_override", _async_user_8
    )
    app, conn = _app
    # First fetchval: source check → "user" (not zotero)
    conn.fetchval.return_value = "user"
    # The authorship SELECT returns None (note belongs to user 7, not 8)
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/notes/1")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PI-A — promote_zotero_note IDOR: cross-tenant write must 404, not promote.
# ---------------------------------------------------------------------------


async def test_promote_zotero_note_rejects_non_author(_app, monkeypatch):
    """PI-A: promote returns 404 when caller is not the note author.

    User 7 owns a Zotero note on a shared-corpus paper (discovered_by IS NULL,
    so assert_paper_ownership would otherwise pass for anyone). User 8 attempts
    to promote it: the user-scoped initial SELECT returns None → 404, before
    any verification or UPDATE runs.
    """
    monkeypatch.setattr(
        "paper_ingestion.routers.notes.current_user_id_strict_with_owner_override", _async_user_8
    )
    app, conn = _app
    # User-scoped "SELECT * FROM paper_notes WHERE id=$1 AND user_id=$2" misses
    # (note belongs to user 7, caller is user 8) → None.
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/notes/5/promote")

    assert resp.status_code == 404
    assert "5" in resp.json()["detail"]
    # The scoped SELECT carried an exact user_id predicate bound to the caller.
    first_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "user_id = $2" in first_sql
    assert conn.fetchrow.await_args_list[0].args[2] == 8
    # No verification / UPDATE was attempted (cross-tenant write blocked).
    conn.fetch.assert_not_awaited()


async def test_promote_zotero_note_author_succeeds(_app, monkeypatch):
    """PI-A: the owning user can still promote their own Zotero note.

    User 7 promotes their own note; the user-scoped SELECT matches and the
    happy path proceeds to a verified promotion.
    """
    monkeypatch.setattr(
        "paper_ingestion.routers.notes.current_user_id_strict_with_owner_override", _async_user_7
    )
    app, conn = _app
    promoted_at = datetime(2026, 1, 5, tzinfo=UTC)
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
    assert body["promoted_at"] is not None
    # Initial SELECT was user-scoped to the owning caller.
    first_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "user_id = $2" in first_sql
    assert conn.fetchrow.await_args_list[0].args[2] == 7
