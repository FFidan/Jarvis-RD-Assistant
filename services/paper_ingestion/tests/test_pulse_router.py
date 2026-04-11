"""Tests for app.routers.pulse — Pulse REST endpoints.

Uses a minimal FastAPI app that mounts only the pulse router so we do not
trigger the full main.py lifespan.  All downstream collaborators
(run_pulse, load_today, load_history, etc.) are patched via monkeypatch.
"""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models import PulseCardResponse, PulseDeckResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common import verify_api_key

from tests.conftest import FakeRecord, _make_pool_and_conn


def _make_deck_response(cards: int = 2) -> PulseDeckResponse:
    return PulseDeckResponse(
        deck_id=1,
        deck_date=date(2026, 4, 10),
        card_count=cards,
        generated_at=datetime(2026, 4, 10, 4, 0, tzinfo=UTC),
        stats={"candidate_count": 42, "duration_s": 1.23, "last_error": None},
        cards=[
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
                reasoning="because relevant",
                signals={"embedding": 0.8, "topic": 0.6},
            )
            for i in range(cards)
        ],
    )


@pytest.fixture
def client():
    """Minimal FastAPI app mounting only the pulse router."""
    from app.deps import limiter
    from app.routers import pulse as pulse_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()

    app.include_router(pulse_router.router)

    app.dependency_overrides[verify_api_key] = lambda: None

    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc, pool, conn

    app.dependency_overrides.clear()


def test_generate_calls_run_pulse(client):
    tc, pool, conn = client
    deck = _make_deck_response()

    with (
        patch("app.routers.pulse.run_pulse", AsyncMock(return_value={"candidate_count": 5})) as mp,
        patch("app.routers.pulse.load_today", AsyncMock(return_value=deck)),
    ):
        resp = tc.post("/api/pulse/generate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["deck_id"] == 1
    assert body["card_count"] == 2
    mp.assert_awaited_once()


def test_today_404_when_no_deck(client):
    tc, _pool, _conn = client
    with patch("app.routers.pulse.load_today", AsyncMock(return_value=None)):
        resp = tc.get("/api/pulse/today")
    assert resp.status_code == 404


def test_today_returns_deck(client):
    tc, _pool, _conn = client
    deck = _make_deck_response(cards=3)
    with patch("app.routers.pulse.load_today", AsyncMock(return_value=deck)):
        resp = tc.get("/api/pulse/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["card_count"] == 3
    assert len(body["cards"]) == 3


def test_history_returns_list(client):
    tc, _pool, _conn = client
    decks = [_make_deck_response(), _make_deck_response(cards=1)]
    with patch("app.routers.pulse.load_history", AsyncMock(return_value=decks)) as m:
        resp = tc.get("/api/pulse/history?days=14")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    m.assert_awaited_once()
    kwargs = m.await_args.kwargs
    assert kwargs.get("days") == 14 or (len(m.await_args.args) >= 2 and m.await_args.args[1] == 14)


def test_rate_persists_rating(client):
    tc, pool, conn = client
    conn.execute.return_value = "INSERT 0 1"

    resp = tc.post("/api/pulse/rate", json={"paper_id": 42, "rating": "up"})
    assert resp.status_code == 200
    conn.execute.assert_awaited()
    call_sql = conn.execute.await_args.args[0]
    assert "INSERT INTO pulse_ratings" in call_sql
    # Verify arguments include paper_id and rating
    args = conn.execute.await_args.args
    assert 42 in args
    assert "up" in args


def test_rate_rejects_invalid_rating(client):
    tc, _pool, _conn = client
    resp = tc.post("/api/pulse/rate", json={"paper_id": 42, "rating": "bogus"})
    assert resp.status_code == 422


def test_explain_returns_signals(client):
    tc, pool, conn = client
    conn.fetchrow.return_value = FakeRecord(
        {
            "id": 7,
            "reasoning": "very relevant to ML",
            "signals": {"embedding": 0.9, "topic": 0.7},
            "llm_relevance": 8,
            "llm_novelty": 6,
        }
    )

    resp = tc.get("/api/pulse/explain/7")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reasoning"] == "very relevant to ML"
    assert body["signals"]["embedding"] == 0.9
    assert body["llm_relevance"] == 8
    assert body["llm_novelty"] == 6


def test_explain_404_on_missing_card(client):
    tc, _pool, conn = client
    conn.fetchrow.return_value = None

    resp = tc.get("/api/pulse/explain/999")
    assert resp.status_code == 404


def test_stats_null_safe_empty_window(client):
    tc, _pool, conn = client
    conn.fetchrow.return_value = FakeRecord(
        {
            "decks_generated": 0,
            "avg_candidates": None,
            "avg_llm_calls": None,
            "avg_duration_s": None,
            "last_run_at": None,
            "last_error": None,
        }
    )

    resp = tc.get("/api/pulse/stats?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decks_generated"] == 0
    assert body["avg_candidates"] is None
    assert body["last_run_at"] is None
    assert body["window_days"] == 30


@pytest.mark.skip(reason="slowapi rate limit state is hard to test deterministically")
def test_generate_rate_limited():
    pass
