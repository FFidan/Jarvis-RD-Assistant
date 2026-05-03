"""Direct tests for weekly summary structured-output behavior."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from paper_ingestion.weekly_summary import generate_weekly_summary
from paper_ingestion.weekly_summary_models import WeeklyDigestOutput


def _make_pool(rows: list[dict]) -> MagicMock:
    """Build a pool mock whose acquire context yields a conn with fetch()."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)

    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


def _row(paper_id: int, topic_name: str) -> dict:
    """Build the minimal weekly summary query row shape."""
    return {
        "id": paper_id,
        "title": f"Paper {paper_id}",
        "url": f"https://example.com/{paper_id}",
        "published_date": datetime(2026, 3, 1, tzinfo=UTC),
        "authors": ["Ada"],
        "topic_name": topic_name,
        "topic_id": 1,
        "relevance_score": 0.9,
        "summary_brief": f"Finding {paper_id}",
        "confidence": "HIGH",
    }


def _mock_openai_client() -> MagicMock:
    """Return a minimal AsyncOpenAI-like mock acceptable to call_llm_structured."""
    client = MagicMock()
    client.chat = MagicMock()
    return client


def _digest_output(summary: str = "ok summary with enough words here") -> WeeklyDigestOutput:
    return WeeklyDigestOutput(themes=[], summary=summary)


async def test_generate_weekly_summary_returns_topics():
    """Digest returns per-topic output keyed by topic_name."""
    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    digest = _digest_output()

    with patch(
        "paper_ingestion.weekly_summary.call_llm_structured",
        new_callable=AsyncMock,
        return_value=digest,
    ):
        result = await generate_weekly_summary(
            pool,
            AsyncMock(),
            openai_client=_mock_openai_client(),
        )

    assert "topics" in result
    assert result["topics"][0]["summary"] == digest.summary


async def test_generate_weekly_summary_uses_structured_output():
    """Digest calls call_llm_structured exactly once per topic group."""
    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    digest = _digest_output()

    with patch(
        "paper_ingestion.weekly_summary.call_llm_structured",
        new_callable=AsyncMock,
        return_value=digest,
    ) as mock_call:
        await generate_weekly_summary(
            pool,
            AsyncMock(),
            openai_client=_mock_openai_client(),
        )

    # One call per topic group (single "NLP" group here).
    assert mock_call.call_count == 1


async def test_generate_weekly_summary_honors_explicit_base_url_override(monkeypatch):
    """Digest passes the litellm_config derived from litellm_url override to call_llm_structured."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://env-url:4000")
    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    digest = _digest_output()

    with patch(
        "paper_ingestion.weekly_summary.call_llm_structured",
        new_callable=AsyncMock,
        return_value=digest,
    ) as mock_call:
        await generate_weekly_summary(
            pool,
            AsyncMock(),
            litellm_url="http://arg-url:4000",
            openai_client=_mock_openai_client(),
        )

    # config kwarg must reflect the explicit override URL
    call_kwargs = mock_call.call_args.kwargs
    assert call_kwargs["config"].base_url == "http://arg-url:4000"


async def test_generate_weekly_summary_default_argument_still_overrides_env(monkeypatch):
    """litellm_url arg overrides env even when it matches the default literal."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://env-url:4000")
    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    digest = _digest_output()

    with patch(
        "paper_ingestion.weekly_summary.call_llm_structured",
        new_callable=AsyncMock,
        return_value=digest,
    ) as mock_call:
        await generate_weekly_summary(
            pool,
            AsyncMock(),
            litellm_url="http://litellm:4000",
            openai_client=_mock_openai_client(),
        )

    call_kwargs = mock_call.call_args.kwargs
    assert call_kwargs["config"].base_url == "http://litellm:4000"
