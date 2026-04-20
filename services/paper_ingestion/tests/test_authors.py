"""Tests for author tracking CRUD endpoints and matching utilities."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common import normalize_author_name
from paper_ingestion.models import (
    AutoDetectResponse,
    TrackedAuthorCreate,
    TrackedAuthorResponse,
    TrackedAuthorUpdate,
)
from paper_ingestion.routers.authors import author_matches

# ---------------------------------------------------------------------------
# Unit tests: normalize_author_name
# ---------------------------------------------------------------------------


def test_normalize_basic():
    """Lowercase and remove periods."""
    assert normalize_author_name("J. Smith") == "j smith"


def test_normalize_extra_spaces():
    """Collapse multiple spaces."""
    assert normalize_author_name("  John   Smith  ") == "john smith"


def test_normalize_initials():
    """Handle initials with periods."""
    assert normalize_author_name("J.R. Smith") == "jr smith"


def test_normalize_no_change_needed():
    """Already normalized name passes through."""
    assert normalize_author_name("john smith") == "john smith"


def test_normalize_mixed_case():
    """Mixed case is lowered."""
    assert normalize_author_name("JOHN SMITH") == "john smith"


def test_normalize_single_name():
    """Single-part names are handled."""
    assert normalize_author_name("Aristotle") == "aristotle"


# ---------------------------------------------------------------------------
# Unit tests: author_matches
# ---------------------------------------------------------------------------


def test_match_exact():
    """Exact names match."""
    assert author_matches("John Smith", "John Smith") is True


def test_match_case_insensitive():
    """Case-insensitive matching."""
    assert author_matches("john smith", "JOHN SMITH") is True


def test_match_initial_vs_full():
    """Initial vs full first name match."""
    assert author_matches("J. Smith", "John Smith") is True


def test_match_full_vs_initial():
    """Full first name vs initial match (reversed)."""
    assert author_matches("John Smith", "J. Smith") is True


def test_no_match_different_last_name():
    """Different last names do not match."""
    assert author_matches("John Smith", "John Jones") is False


def test_no_match_different_initial():
    """Different first initials do not match."""
    assert author_matches("J. Smith", "Robert Smith") is False


def test_no_match_single_name():
    """Single-part names only match exactly."""
    assert author_matches("Aristotle", "Plato") is False


def test_match_single_name_exact():
    """Single-part names match exactly."""
    assert author_matches("Aristotle", "Aristotle") is True


def test_match_with_periods_and_spaces():
    """Periods and extra spaces handled in matching."""
    assert author_matches("J.  Smith", "j smith") is True


# ---------------------------------------------------------------------------
# Pydantic model tests
# ---------------------------------------------------------------------------


def test_tracked_author_create_valid():
    """TrackedAuthorCreate accepts valid data."""
    body = TrackedAuthorCreate(author_name="John Smith", s2_author_id="12345")
    assert body.author_name == "John Smith"
    assert body.s2_author_id == "12345"


def test_tracked_author_create_no_s2_id():
    """TrackedAuthorCreate works without S2 ID."""
    body = TrackedAuthorCreate(author_name="John Smith")
    assert body.s2_author_id is None


def test_tracked_author_create_rejects_empty_name():
    """TrackedAuthorCreate rejects empty author_name."""
    with pytest.raises(Exception):
        TrackedAuthorCreate(author_name="")


def test_tracked_author_update_partial():
    """TrackedAuthorUpdate supports partial updates."""
    body = TrackedAuthorUpdate(enabled=False)
    dump = body.model_dump(exclude_unset=True)
    assert dump == {"enabled": False}
    assert "s2_author_id" not in dump


def test_tracked_author_response_from_dict():
    """TrackedAuthorResponse parses a typical row dict."""
    now = datetime.now(tz=UTC)
    resp = TrackedAuthorResponse(
        id=1,
        author_name="John Smith",
        s2_author_id="12345",
        source="manual",
        enabled=True,
        last_checked_at=None,
        created_at=now,
    )
    assert resp.id == 1
    assert resp.author_name == "John Smith"
    assert resp.s2_author_id == "12345"


def test_auto_detect_response():
    """AutoDetectResponse validates correctly."""
    now = datetime.now(tz=UTC)
    resp = AutoDetectResponse(
        added=2,
        already_tracked=1,
        authors=[
            TrackedAuthorResponse(
                id=1,
                author_name="Alice",
                s2_author_id=None,
                source="auto_starred",
                enabled=True,
                last_checked_at=None,
                created_at=now,
            )
        ],
    )
    assert resp.added == 2
    assert len(resp.authors) == 1


# ---------------------------------------------------------------------------
# Endpoint integration tests (mocked DB)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_author_record(
    author_id: int = 1,
    author_name: str = "John Smith",
    s2_author_id: str | None = None,
    source: str = "manual",
    enabled: bool = True,
) -> dict:
    """Build a dict mimicking an asyncpg.Record for tracked_authors."""
    return {
        "id": author_id,
        "author_name": author_name,
        "s2_author_id": s2_author_id,
        "source": source,
        "enabled": enabled,
        "last_checked_at": None,
        "created_at": _NOW,
    }


def _mock_pool() -> tuple[AsyncMock, AsyncMock]:
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
    """Create a minimal app instance with mocked state."""
    from paper_ingestion.main import app

    pool, conn = _mock_pool()
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    return app, conn


async def test_list_authors_empty(_app):
    """GET /api/authors returns empty list when none exist."""
    app, conn = _app
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/authors")

    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_authors_returns_data(_app):
    """GET /api/authors returns tracked authors."""
    app, conn = _app
    conn.fetch.return_value = [
        _make_author_record(author_id=1, author_name="Alice"),
        _make_author_record(author_id=2, author_name="Bob"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/authors")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["author_name"] == "Alice"


async def test_create_author_returns_201(_app):
    """POST /api/authors creates and returns author with 201."""
    app, conn = _app
    conn.fetchrow.side_effect = [
        None,  # no duplicate
        _make_author_record(author_name="Jane Doe"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/authors",
            json={"author_name": "Jane Doe"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["author_name"] == "Jane Doe"
    assert body["source"] == "manual"


async def test_create_author_with_s2_id(_app):
    """POST /api/authors stores S2 author ID in the record."""
    app, conn = _app
    conn.fetchrow.side_effect = [
        None,  # no duplicate
        _make_author_record(author_name="Jane Doe", s2_author_id="S2_123"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/authors",
            json={"author_name": "Jane Doe", "s2_author_id": "S2_123"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["s2_author_id"] == "S2_123"


async def test_create_duplicate_author_returns_409(_app):
    """POST /api/authors returns 409 for duplicate author."""
    app, conn = _app
    conn.fetchrow.return_value = {"id": 1}  # already exists

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/authors",
            json={"author_name": "John Smith"},
        )

    assert resp.status_code == 409


async def test_update_author(_app):
    """PUT /api/authors/{id} updates author fields."""
    app, conn = _app

    with patch(
        "paper_ingestion.routers.authors.dynamic_update",
        new_callable=AsyncMock,
        return_value=_make_author_record(enabled=False),
    ):
        conn.fetchrow.return_value = _make_author_record()

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/authors/1",
                json={"enabled": False},
            )

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


async def test_update_author_not_found(_app):
    """PUT /api/authors/{id} returns 404 for missing author."""
    app, conn = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/authors/999",
            json={"enabled": False},
        )

    assert resp.status_code == 404


async def test_delete_author_returns_204(_app):
    """DELETE /api/authors/{id} returns 204 on success."""
    app, conn = _app

    with patch("paper_ingestion.routers.authors.delete_or_404", new_callable=AsyncMock):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/authors/1")

    assert resp.status_code == 204


async def test_delete_author_not_found(_app):
    """DELETE /api/authors/{id} returns 404 when author does not exist."""
    from fastapi import HTTPException

    app, conn = _app

    async def _raise_404(*args, **kwargs):
        raise HTTPException(status_code=404, detail="Not found")

    with patch("paper_ingestion.routers.authors.delete_or_404", side_effect=_raise_404):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/authors/999")

    assert resp.status_code == 404


async def test_auto_detect_from_starred(_app):
    """POST /api/authors/auto-detect detects authors from starred papers."""
    app, conn = _app
    conn.fetch.return_value = [
        {"author_name": "Alice Researcher", "is_starred": True, "max_rating": None},
        {"author_name": "Bob Scientist", "is_starred": False, "max_rating": 5},
    ]
    # First call: check existing, second call: insert
    conn.fetchrow.side_effect = [
        None,  # Alice not tracked
        _make_author_record(author_name="Alice Researcher", source="auto_starred"),
        None,  # Bob not tracked
        _make_author_record(author_name="Bob Scientist", source="auto_rated"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/authors/auto-detect")

    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] == 2
    assert body["already_tracked"] == 0
    assert len(body["authors"]) == 2


async def test_auto_detect_skips_existing(_app):
    """POST /api/authors/auto-detect skips already-tracked authors."""
    app, conn = _app
    conn.fetch.return_value = [
        {"author_name": "Alice Researcher", "is_starred": True, "max_rating": None},
    ]
    conn.fetchrow.return_value = {"id": 1}  # already tracked

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/authors/auto-detect")

    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] == 0
    assert body["already_tracked"] == 1


async def test_check_authors_finds_matches(_app):
    """POST /api/authors/check finds matching papers."""
    app, conn = _app
    # First fetch: tracked authors, second fetch: recent papers
    conn.fetch.side_effect = [
        [_make_author_record(author_name="John Smith")],
        [
            {
                "id": 42,
                "authors": ["John Smith", "Jane Doe"],
                "metadata": {},
            }
        ],
    ]
    conn.execute.side_effect = [
        "UPDATE 1",  # last_checked_at
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/authors/check")

    assert resp.status_code == 200
    body = resp.json()
    assert body["new_papers"] == 1
    assert body["authors_checked"] == 1


async def test_check_authors_s2_id_match(_app):
    """POST /api/authors/check matches by S2 author ID."""
    app, conn = _app
    conn.fetch.side_effect = [
        [_make_author_record(author_name="John Smith", s2_author_id="S2_99")],
        [
            {
                "id": 42,
                "authors": ["J. Q. Smith"],
                "metadata": {"s2_author_ids": [{"name": "J. Q. Smith", "authorId": "S2_99"}]},
            }
        ],
    ]
    conn.execute.side_effect = [
        "UPDATE 1",  # last_checked_at
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/authors/check")

    assert resp.status_code == 200
    body = resp.json()
    assert body["new_papers"] == 1


async def test_check_authors_no_enabled(_app):
    """POST /api/authors/check returns zeros when no authors enabled."""
    app, conn = _app
    conn.fetch.return_value = []  # no enabled authors

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/authors/check")

    assert resp.status_code == 200
    body = resp.json()
    assert body["new_papers"] == 0
    assert body["authors_checked"] == 0
