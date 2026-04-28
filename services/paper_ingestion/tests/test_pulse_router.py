"""Tests for app.routers.pulse — Pulse REST endpoints.

Uses a minimal FastAPI app that mounts only the pulse router so we do not
trigger the full main.py lifespan.  All downstream collaborators
(run_pulse, load_today, load_history, etc.) are patched via monkeypatch.
"""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common import verify_api_key
from paper_ingestion.models import PulseCardResponse, PulseDeckResponse
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

    with TestClient(app, raise_server_exceptions=False, backend_options={"use_uvloop": True}) as tc:
        yield tc, pool, conn

    app.dependency_overrides.clear()
    limiter.enabled = True  # restore global limiter state for subsequent tests


def test_generate_returns_job_id(client):
    """POST /generate now enqueues a job and returns {job_id, status}."""
    tc, pool, conn = client

    with patch(
        "paper_ingestion.routers.pulse.jobs_lib.enqueue",
        AsyncMock(return_value="test-job-uuid-1234"),
    ) as mp:
        resp = tc.post("/api/pulse/generate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "test-job-uuid-1234"
    assert body["status"] == "queued"
    mp.assert_awaited_once()


def test_today_404_when_no_deck(client):
    tc, _pool, _conn = client
    with patch("paper_ingestion.routers.pulse.load_today", AsyncMock(return_value=None)):
        resp = tc.get("/api/pulse/today")
    assert resp.status_code == 404


def test_today_returns_deck(client):
    tc, _pool, _conn = client
    deck = _make_deck_response(cards=3)
    with patch("paper_ingestion.routers.pulse.load_today", AsyncMock(return_value=deck)):
        resp = tc.get("/api/pulse/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["card_count"] == 3
    assert len(body["cards"]) == 3


def test_history_returns_list(client):
    tc, _pool, _conn = client
    decks = [_make_deck_response(), _make_deck_response(cards=1)]
    with patch("paper_ingestion.routers.pulse.load_history", AsyncMock(return_value=decks)) as m:
        resp = tc.get("/api/pulse/history?days=14")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    m.assert_awaited_once()
    call = m.await_args
    assert call is not None
    assert call.kwargs.get("days") == 14 or (len(call.args) >= 2 and call.args[1] == 14)


def test_rate_persists_rating(client):
    tc, pool, conn = client
    conn.execute.return_value = "INSERT 0 1"

    resp = tc.post("/api/pulse/rate", json={"paper_id": 42, "rating": "up"})
    assert resp.status_code == 200
    # Sprint 7 B3: rate_card now issues TWO writes — pulse_ratings then
    # paper_user_state preference sync. Inspect both rather than only the
    # last awaited call.
    call_sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert any("INSERT INTO pulse_ratings" in sql for sql in call_sqls)
    rating_args = next(
        c.args for c in conn.execute.await_args_list if "INSERT INTO pulse_ratings" in c.args[0]
    )
    assert 42 in rating_args
    assert "up" in rating_args


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
            "degraded_reason": None,
        }
    )

    resp = tc.get("/api/pulse/stats?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decks_generated"] == 0
    assert body["avg_candidates"] is None
    assert body["last_run_at"] is None
    assert body["window_days"] == 30
    assert body["degraded_reason"] is None


@pytest.mark.skip(reason="slowapi rate limit state is hard to test deterministically")
def test_generate_rate_limited():
    pass


# ---------------------------------------------------------------------------
# GET /api/pulse/debug
# ---------------------------------------------------------------------------


def test_debug_returns_expected_shape_when_deck_exists(client):
    """GET /debug returns 200 with source_counts, topic_embeddings, top_cards."""
    tc, pool, conn = client
    from datetime import date, datetime

    from tests.conftest import FakeRecord

    deck_row = FakeRecord(
        {
            "id": 5,
            "deck_date": date(2026, 4, 10),
            "card_count": 2,
            "generated_at": datetime(2026, 4, 10, 4, 0, tzinfo=None),
            "stats": {"candidate_count": 80, "source_counts": {"arxiv": 50, "pubmed": 30}},
            "degraded_reason": None,
        }
    )
    card_rows = [
        FakeRecord(
            {
                "card_id": 1,
                "paper_id": 101,
                "paper_title": "Paper Alpha",
                "rank": 1,
                "final_score": 0.92,
                "llm_relevance": 9,
                "llm_novelty": 7,
                "signals": {"embedding": 0.88, "topic": 0.75},
            }
        ),
        FakeRecord(
            {
                "card_id": 2,
                "paper_id": 102,
                "paper_title": "Paper Beta",
                "rank": 2,
                "final_score": 0.85,
                "llm_relevance": 8,
                "llm_novelty": 6,
                "signals": {"embedding": 0.80, "topic": 0.70},
            }
        ),
    ]
    embed_rows = [
        FakeRecord({"key": "topic.1.embedding", "value": [0.1] * 768}),
    ]

    # conn.fetchrow returns deck_row on first call; conn.fetch returns cards then embeds
    fetch_calls = [card_rows, embed_rows]
    fetch_iter = iter(fetch_calls)

    conn.fetchrow.return_value = deck_row
    conn.fetch.side_effect = lambda *_a, **_k: fetch_iter.__next__()

    resp = tc.get("/api/pulse/debug")
    assert resp.status_code == 200
    body = resp.json()

    # Top-level keys
    assert "deck_date" in body
    assert "card_count" in body
    assert "source_counts" in body
    assert "topic_embeddings" in body
    assert "top_cards" in body

    assert body["card_count"] == 2
    assert body["source_counts"] == {"arxiv": 50, "pubmed": 30}

    # Topic embedding sanity
    assert len(body["topic_embeddings"]) == 1
    emb = body["topic_embeddings"][0]
    assert emb["dim"] == 768
    assert emb["ok"] is True
    assert emb["non_null"] is True

    # Cards
    assert len(body["top_cards"]) == 2
    card = body["top_cards"][0]
    assert card["paper_id"] == 101
    assert card["title"] == "Paper Alpha"
    assert "signals" in card
    assert card["final_score"] == pytest.approx(0.92)


def test_debug_404_when_no_deck(client):
    """GET /debug returns 404 when no deck exists."""
    tc, pool, conn = client
    conn.fetchrow.return_value = None
    resp = tc.get("/api/pulse/debug")
    assert resp.status_code == 404


def test_today_surfaces_degraded_reason_at_top_level(client):
    """GET /today returns degraded_reason as a top-level typed field.

    Loads the deck via the real load_today() path (no mock) so we exercise
    the SELECT list + _build_deck_response pipeline end-to-end.
    """
    tc, pool, conn = client
    from datetime import date, datetime

    from tests.conftest import FakeRecord

    deck_row = FakeRecord(
        {
            "id": 9,
            "deck_date": date(2026, 4, 10),
            "card_count": 0,
            "generated_at": datetime(2026, 4, 10, 4, 0, tzinfo=None),
            "stats": {"candidate_count": 0},
            "degraded_reason": "Stage 2 verifier timeout",
        }
    )
    conn.fetchrow.return_value = deck_row
    conn.fetch.return_value = []

    resp = tc.get("/api/pulse/today")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded_reason"] == "Stage 2 verifier timeout"


def test_stats_includes_degraded_reason(client):
    """GET /stats returns degraded_reason from the typed DB column."""
    tc, pool, conn = client
    from datetime import datetime

    conn.fetchrow.return_value = FakeRecord(
        {
            "decks_generated": 1,
            "avg_candidates": 80.0,
            "avg_llm_calls": 0.0,
            "avg_duration_s": 120.0,
            "last_run_at": datetime(2026, 4, 10, 4, 0),
            "last_error": None,
            "degraded_reason": "LLM scoring timed out at 600s; deck used embedding-only fallback.",
        }
    )

    resp = tc.get("/api/pulse/stats?days=30")
    assert resp.status_code == 200
    body = resp.json()
    expected_dr = "LLM scoring timed out at 600s; deck used embedding-only fallback."
    assert body["degraded_reason"] == expected_dr
    assert body["last_error"] is None
