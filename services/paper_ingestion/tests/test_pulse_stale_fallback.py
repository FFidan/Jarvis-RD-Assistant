"""Tests for the stale-fallback logic in GET /api/pulse/today.

Covers:
- Today's deck has cards → returned as-is (is_stale=False).
- Today's deck is empty AND a recent non-empty deck exists → stale fallback
  with is_stale=True, stale_age_days set, stale_diagnostics populated.
- Today's deck is empty AND no recent non-empty deck → empty_reason="no_data_yet".
- Stale response includes source_health diagnostics.
"""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common import get_current_user_id, verify_api_key
from paper_ingestion.models import PulseCardResponse, PulseDeckResponse
from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
THREE_DAYS_AGO = TODAY - timedelta(days=3)


def _make_deck(card_count: int = 2, deck_date: date = TODAY) -> PulseDeckResponse:
    """Build a minimal PulseDeckResponse for patching."""
    cards = [
        PulseCardResponse(
            card_id=i + 1,
            paper_id=100 + i,
            paper_title=f"Paper {i}",
            paper_authors=["Alice"],
            paper_url=f"https://example.com/{i}",
            rank=i + 1,
            score=0.9 - 0.1 * i,
            llm_relevance=8,
            llm_novelty=6,
            reasoning="relevant",
            signals={"embedding": 0.8},
        )
        for i in range(card_count)
    ]
    return PulseDeckResponse(
        deck_id=1,
        deck_date=deck_date,
        card_count=card_count,
        generated_at=datetime(deck_date.year, deck_date.month, deck_date.day, 4, 0, tzinfo=UTC),
        stats={"candidate_count": 42},
        cards=cards,
    )


def _make_fallback_row(deck_date: date = YESTERDAY, card_count: int = 3) -> PulseDeckResponse:
    """Build a fallback PulseDeckResponse (load_last_nonempty_deck returns a full deck)."""
    return _make_deck(card_count=card_count, deck_date=deck_date)


def _make_health_rows() -> list[FakeRecord]:
    """Build sample source_health FakeRecord rows."""
    return [
        FakeRecord(
            {
                "source_type": "arxiv",
                "last_status": "rate_limit",
                "cooldown_until": datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
                "consecutive_failures": 3,
            }
        ),
        FakeRecord(
            {
                "source_type": "pubmed",
                "last_status": "ok",
                "cooldown_until": None,
                "consecutive_failures": 0,
            }
        ),
    ]


@pytest.fixture()
def client():
    """Minimal FastAPI app mounting only the pulse router."""
    from paper_ingestion.deps import limiter
    from paper_ingestion.routers import pulse as pulse_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()

    app.include_router(pulse_router.router)

    async def override_api_key():
        return None

    app.dependency_overrides[verify_api_key] = override_api_key
    # CC-03: this fixture builds its own FastAPI app (not paper_ingestion.main.app),
    # so the autouse ``_default_authenticated_user`` override does not reach it.
    # Add it here so the converted ``Depends(get_current_user_id)`` pulse routes
    # default to user 1 (identical to the pre-conversion symbol-stub behaviour).
    app.dependency_overrides[get_current_user_id] = lambda: 1

    with TestClient(app, raise_server_exceptions=False, backend_options={"use_uvloop": True}) as tc:
        yield tc, pool, conn

    app.dependency_overrides.clear()
    limiter.enabled = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_today_returns_today_when_card_count_positive(client):
    """When today's deck has cards, return it as-is with is_stale=False."""
    tc, _pool, _conn = client
    deck = _make_deck(card_count=3)

    with patch("paper_ingestion.routers.pulse.load_today", AsyncMock(return_value=deck)):
        resp = tc.get("/api/pulse/today")

    assert resp.status_code == 200
    body = resp.json()
    assert body["card_count"] == 3
    assert body["is_stale"] is False
    assert body["stale_age_days"] is None
    assert body["stale_diagnostics"] is None
    assert body["empty_reason"] is None


def test_get_today_returns_yesterday_with_is_stale_when_today_empty(client):
    """When today's deck has 0 cards and a recent non-empty deck exists, return stale=True."""
    tc, pool, conn = client

    empty_today = _make_deck(card_count=0)
    fallback_row = _make_fallback_row(deck_date=YESTERDAY, card_count=2)

    # conn.fetch is called for source_health in the stale path
    conn.fetch.return_value = []

    with (
        patch("paper_ingestion.routers.pulse.load_today", AsyncMock(return_value=empty_today)),
        patch(
            "paper_ingestion.routers.pulse.load_last_nonempty_deck",
            AsyncMock(return_value=fallback_row),
        ),
    ):
        resp = tc.get("/api/pulse/today")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_stale"] is True
    assert body["stale_age_days"] == 1  # YESTERDAY is 1 day ago
    assert body["empty_reason"] is None  # not set when a fallback was found


def test_get_today_returns_empty_with_no_data_yet_when_no_recent_decks(client):
    """When today's deck is empty and no recent non-empty deck exists, set empty_reason."""
    tc, _pool, _conn = client
    empty_today = _make_deck(card_count=0)

    with (
        patch("paper_ingestion.routers.pulse.load_today", AsyncMock(return_value=empty_today)),
        patch(
            "paper_ingestion.routers.pulse.load_last_nonempty_deck",
            AsyncMock(return_value=None),
        ),
    ):
        resp = tc.get("/api/pulse/today")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_stale"] is False
    assert body["empty_reason"] == "no_data_yet"
    assert body["stale_age_days"] is None
    assert body["stale_diagnostics"] is None


def test_get_today_includes_source_diagnostics_in_stale_response(client):
    """Stale response carries stale_diagnostics from source_health rows."""
    tc, pool, conn = client

    empty_today = _make_deck(card_count=0)
    fallback_row = _make_fallback_row(deck_date=THREE_DAYS_AGO, card_count=5)
    health_rows = _make_health_rows()

    # conn.fetch is used for the source_health query inside the handler
    conn.fetch.return_value = health_rows

    with (
        patch("paper_ingestion.routers.pulse.load_today", AsyncMock(return_value=empty_today)),
        patch(
            "paper_ingestion.routers.pulse.load_last_nonempty_deck",
            AsyncMock(return_value=fallback_row),
        ),
    ):
        resp = tc.get("/api/pulse/today")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_stale"] is True
    assert body["stale_age_days"] == 3
    diag = body["stale_diagnostics"]
    assert diag is not None
    assert "arxiv" in diag
    assert diag["arxiv"]["last_status"] == "rate_limit"
    assert diag["arxiv"]["consecutive_failures"] == 3
    assert diag["arxiv"]["cooldown_until"] is not None
    assert "pubmed" in diag
    assert diag["pubmed"]["last_status"] == "ok"
    assert diag["pubmed"]["cooldown_until"] is None
    assert diag["pubmed"]["consecutive_failures"] == 0


# test_get_today_404_when_no_deck_at_all: DELETED (collapsed into contract test).
# Duplicate of test_pulse_router.py::test_today_404_when_no_deck — identical
# setup (patch load_today → None) and assertion (resp.status_code == 404).


def test_load_last_nonempty_deck_not_called_when_today_has_cards(client):
    """load_last_nonempty_deck is never invoked when today's deck is non-empty."""
    tc, _pool, _conn = client
    deck = _make_deck(card_count=2)

    with (
        patch("paper_ingestion.routers.pulse.load_today", AsyncMock(return_value=deck)),
        patch(
            "paper_ingestion.routers.pulse.load_last_nonempty_deck",
            AsyncMock(return_value=None),
        ) as mock_fallback,
    ):
        resp = tc.get("/api/pulse/today")

    assert resp.status_code == 200
    mock_fallback.assert_not_awaited()
