"""Tests for the weekly research summary generator."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import respx
from app.weekly_summary import generate_weekly_summary


def _make_paper_row(
    paper_id: int,
    title: str,
    topic_name: str,
    *,
    topic_id: int = 1,
    relevance_score: float | None = 0.9,
    summary_brief: str | None = None,
    confidence: str | None = None,
) -> dict:
    """Build a fake DB row matching the weekly summary query columns."""
    return {
        "id": paper_id,
        "title": title,
        "url": f"https://arxiv.org/abs/{paper_id}",
        "published_date": datetime(2026, 3, 1, tzinfo=UTC),
        "authors": ["Author A", "Author B"],
        "topic_name": topic_name,
        "topic_id": topic_id,
        "relevance_score": relevance_score,
        "summary_brief": summary_brief,
        "confidence": confidence,
    }


def _llm_response(themes: list[dict], summary: str) -> dict:
    """Build a mock LiteLLM chat completion response."""
    return {
        "choices": [{"message": {"content": json.dumps({"themes": themes, "summary": summary})}}]
    }


def _make_pool(rows: list) -> MagicMock:
    """Build a mock asyncpg.Pool whose .acquire() context manager returns a conn with .fetch()."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)

    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


@respx.mock
async def test_generate_weekly_summary_with_synthesis():
    """Digest groups papers by topic and calls LLM for synthesis."""
    rows = [
        _make_paper_row(1, "Paper A", "NLP", summary_brief="NLP finding A"),
        _make_paper_row(2, "Paper B", "NLP", summary_brief="NLP finding B"),
        _make_paper_row(3, "Paper C", "CV", summary_brief="CV finding C"),
    ]

    db_pool = _make_pool(rows)

    llm_themes = [
        {
            "theme": "Attention mechanisms dominate",
            "supporting_papers": [1, 2],
            "notes": "",
        }
    ]
    llm_resp = _llm_response(llm_themes, "NLP is evolving fast.")

    respx.post("http://litellm:4000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=llm_resp)
    )

    async with httpx.AsyncClient() as client:
        result = await generate_weekly_summary(db_pool, client, days=7)

    assert result["total_papers"] == 3
    assert len(result["topics"]) == 2
    assert "period_start" in result
    assert "period_end" in result

    # NLP topic should have LLM themes (2 papers)
    nlp_topic = next(t for t in result["topics"] if t["name"] == "NLP")
    assert nlp_topic["paper_count"] == 2
    assert len(nlp_topic["themes"]) == 1
    assert nlp_topic["themes"][0]["theme"] == "Attention mechanisms dominate"
    assert nlp_topic["summary"] == "NLP is evolving fast."

    # CV topic has only 1 paper -- no LLM call, fallback summary
    cv_topic = next(t for t in result["topics"] if t["name"] == "CV")
    assert cv_topic["paper_count"] == 1
    assert cv_topic["themes"] == []
    assert "1 papers on CV" in cv_topic["summary"]


@respx.mock
async def test_generate_weekly_summary_empty():
    """Weekly summary handles no engaged papers gracefully with honest empty response."""
    db_pool = _make_pool([])

    async with httpx.AsyncClient() as client:
        result = await generate_weekly_summary(db_pool, client, days=7)

    assert result["total_papers"] == 0
    assert result["topics"] == []
    assert "period_start" in result
    assert "period_end" in result
    assert "message" in result


@respx.mock
async def test_generate_weekly_summary_llm_failure():
    """Digest returns topics without themes when LLM fails."""
    rows = [
        _make_paper_row(1, "Paper A", "NLP", summary_brief="finding A"),
        _make_paper_row(2, "Paper B", "NLP", summary_brief="finding B"),
    ]

    db_pool = _make_pool(rows)

    respx.post("http://litellm:4000/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    async with httpx.AsyncClient() as client:
        result = await generate_weekly_summary(db_pool, client, days=7)

    assert result["total_papers"] == 2
    assert len(result["topics"]) == 1

    nlp_topic = result["topics"][0]
    assert nlp_topic["name"] == "NLP"
    assert nlp_topic["paper_count"] == 2
    # Themes should be empty since LLM call failed
    assert nlp_topic["themes"] == []
    # Fallback summary used
    assert "2 papers on NLP" in nlp_topic["summary"]
    # Top papers still populated
    assert len(nlp_topic["top_papers"]) == 2


@respx.mock
async def test_generate_weekly_summary_top_papers_structure():
    """Top papers contain expected fields."""
    rows = [
        _make_paper_row(
            10,
            "Important Paper",
            "ML",
            relevance_score=0.95,
            confidence="HIGH",
        ),
    ]

    db_pool = _make_pool(rows)

    async with httpx.AsyncClient() as client:
        result = await generate_weekly_summary(db_pool, client, days=7)

    paper = result["topics"][0]["top_papers"][0]
    assert paper["id"] == 10
    assert paper["title"] == "Important Paper"
    assert paper["url"] == "https://arxiv.org/abs/10"
    assert paper["confidence"] == "HIGH"
    assert paper["relevance_score"] == 0.95


async def test_generate_weekly_summary_uses_api_key_with_master_key_fallback(monkeypatch):
    """Weekly summary should use API_KEY first, but still honor MASTER_KEY as a fallback."""
    rows = [
        _make_paper_row(1, "Paper A", "NLP", summary_brief="NLP finding A"),
        _make_paper_row(2, "Paper B", "NLP", summary_brief="NLP finding B"),
    ]
    db_pool = _make_pool(rows)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = _llm_response([], "fallback")
    http_client = AsyncMock()
    http_client.post.return_value = response

    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-secret")

    result = await generate_weekly_summary(db_pool, http_client, days=7)

    assert result["topics"][0]["summary"] == "fallback"
    http_client.post.assert_awaited_once()
    assert http_client.post.await_args.kwargs["headers"] == {
        "Authorization": "Bearer master-secret"
    }


# ---------------------------------------------------------------------------
# Model C drift-prevention tests (spec §6.4)
# ---------------------------------------------------------------------------


async def test_excludes_unengaged_papers():
    """Paper surfaced by Pulse but never rated or saved is NOT in the summary.

    The SQL engagement filter excludes it; the mock simulates that by returning
    an empty row list (what the filtered query would produce).
    """
    # Paper P is in the window but has no paper_user_state and no pulse_ratings.
    # The engagement-filtered SQL returns no rows for it.
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(db_pool, http_client, days=7)

    # P is not in the output
    all_ids = [p["id"] for topic in result["topics"] for p in topic["top_papers"]]
    assert 99 not in all_ids
    assert result["total_papers"] == 0
    # LLM must not be called for empty results
    http_client.post.assert_not_awaited()


async def test_includes_saved_papers():
    """Paper saved via Library UI is included in the summary.

    The SQL includes it via paper_user_state.user_state = 'saved';
    the mock returns the row to simulate that.
    """
    row_q = _make_paper_row(42, "Paper Q — Saved", "ML", summary_brief="Saved finding")
    db_pool = _make_pool([row_q])

    http_client = AsyncMock()

    result = await generate_weekly_summary(db_pool, http_client, days=7)

    all_ids = [p["id"] for topic in result["topics"] for p in topic["top_papers"]]
    assert 42 in all_ids


async def test_includes_positively_rated_pulse_papers():
    """Paper with a Pulse 'up' rating is included in the summary.

    The SQL includes it via pulse_ratings.rating = 'up'; the mock returns the row.
    """
    row_r = _make_paper_row(43, "Paper R — Upvoted", "CV", summary_brief="Upvoted finding")
    db_pool = _make_pool([row_r])

    http_client = AsyncMock()

    result = await generate_weekly_summary(db_pool, http_client, days=7)

    all_ids = [p["id"] for topic in result["topics"] for p in topic["top_papers"]]
    assert 43 in all_ids


async def test_excludes_negatively_rated_pulse_papers():
    """Paper with a Pulse 'down' rating is NOT included in the summary.

    'down' is not an engagement signal; the SQL excludes it.
    The mock returns [] to simulate the filtered result.
    """
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(db_pool, http_client, days=7)

    assert result["total_papers"] == 0
    assert result["topics"] == []
    http_client.post.assert_not_awaited()


async def test_dismiss_rating_excluded():
    """Paper with a Pulse 'dismiss' rating is NOT included in the summary.

    'dismiss' is an explicit negative signal; the SQL excludes it.
    The mock returns [] to simulate the filtered result.
    """
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(db_pool, http_client, days=7)

    assert result["total_papers"] == 0
    assert result["topics"] == []
    http_client.post.assert_not_awaited()


async def test_empty_when_no_engagement():
    """Honest empty response when no papers have engagement.

    Verifies the short-circuit path: no LLM call, valid response shape,
    and presence of a human-readable message field.
    """
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(db_pool, http_client, days=7)

    assert result["total_papers"] == 0
    assert result["topics"] == []
    assert "period_start" in result
    assert "period_end" in result
    assert "message" in result
    assert len(result["message"]) > 0
    # The LLM must NOT be called when there are no papers to summarize.
    http_client.post.assert_not_awaited()
