"""Direct tests for weekly summary LiteLLM helper behavior without respx."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from paper_ingestion.weekly_summary import generate_weekly_summary


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


async def test_generate_weekly_summary_uses_master_key_fallback(monkeypatch):
    """Digest should fall back to MASTER_KEY when API_KEY is unset."""
    http_client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '{"themes": [], "summary": "ok"}'}}]
    }
    http_client.post.return_value = response

    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-secret")

    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    result = await generate_weekly_summary(pool, http_client)

    assert result["topics"][0]["summary"] == "ok"
    http_client.post.assert_awaited_once()
    assert http_client.post.await_args.kwargs["headers"] == {
        "Authorization": "Bearer master-secret"
    }


async def test_generate_weekly_summary_prefers_api_key(monkeypatch):
    """Digest should prefer the standard API key when both env vars are present."""
    http_client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '{"themes": [], "summary": "ok"}'}}]
    }
    http_client.post.return_value = response

    monkeypatch.setenv("LITELLM_API_KEY", "api-secret")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-secret")

    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    await generate_weekly_summary(pool, http_client)

    assert http_client.post.await_args.kwargs["headers"] == {"Authorization": "Bearer api-secret"}


async def test_generate_weekly_summary_honors_explicit_base_url_override(monkeypatch):
    """Digest should let an explicit function argument override the env base URL."""
    http_client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '{"themes": [], "summary": "ok"}'}}]
    }
    http_client.post.return_value = response

    monkeypatch.setenv("LITELLM_BASE_URL", "http://env-url:4000")

    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    await generate_weekly_summary(
        pool,
        http_client,
        litellm_url="http://arg-url:4000",
    )

    assert http_client.post.await_args.args[0] == "http://arg-url:4000/v1/chat/completions"


async def test_generate_weekly_summary_default_argument_still_overrides_env(monkeypatch):
    """Digest should honor the function argument even when it matches the default literal."""
    http_client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '{"themes": [], "summary": "ok"}'}}]
    }
    http_client.post.return_value = response

    monkeypatch.setenv("LITELLM_BASE_URL", "http://env-url:4000")

    pool = _make_pool([_row(1, "NLP"), _row(2, "NLP")])
    await generate_weekly_summary(
        pool,
        http_client,
        litellm_url="http://litellm:4000",
    )

    assert http_client.post.await_args.args[0] == "http://litellm:4000/v1/chat/completions"
