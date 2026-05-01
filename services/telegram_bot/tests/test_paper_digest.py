"""Unit tests for the weekly paper digest workflow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from telegram_bot.config import BotConfig
from telegram_bot.orchestration import paper_digest


def _make_config(api_key: str = "secret") -> BotConfig:
    """Create a minimal bot config for digest tests."""
    return BotConfig(
        telegram_token="token",
        telegram_chat_id=1234,
        database_url="postgres://example",
        paper_ingestion_url="http://paper-ingestion:8000",
        learning_engine_url="http://learning-engine:8001",
        jarvis_api_key=api_key,
    )


@pytest.mark.asyncio
async def test_fetch_digest_from_api_returns_payload_and_auth_header():
    """The digest fetch helper returns parsed JSON and includes the API key header."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"topics": [{"name": "LLMs"}]}
    http_client.get.return_value = response

    result = await paper_digest._fetch_digest_from_api(http_client, _make_config())

    assert result == {"topics": [{"name": "LLMs"}]}
    _, kwargs = http_client.get.await_args
    assert kwargs["headers"]["X-API-Key"] == "secret"


@pytest.mark.asyncio
async def test_fetch_digest_from_api_returns_none_on_error():
    """HTTP failures degrade to None so the caller can fall back."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = httpx.ConnectError("offline")

    result = await paper_digest._fetch_digest_from_api(http_client, _make_config())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_digest_from_api_omits_auth_header_without_api_key():
    """Empty bot API keys should not emit an auth header."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"topics": []}
    http_client.get.return_value = response

    result = await paper_digest._fetch_digest_from_api(http_client, _make_config(api_key=""))

    assert result == {"topics": []}
    _, kwargs = http_client.get.await_args
    assert kwargs["headers"] == {}


@pytest.mark.asyncio
async def test_send_chunked_sends_single_message_when_short():
    """Short digests are sent as a single Telegram message."""
    bot = AsyncMock()

    await paper_digest._send_chunked(bot, 1234, ["line one", "line two"])

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 1234
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_chunked_splits_long_messages():
    """Long digests are split into multiple Telegram-sized chunks."""
    bot = AsyncMock()
    lines = [f"line-{i}-{'x' * 1000}" for i in range(5)]

    await paper_digest._send_chunked(bot, 1234, lines)

    assert bot.send_message.await_count >= 2
    sent_text = "\n".join(call.kwargs["text"] for call in bot.send_message.await_args_list)
    assert "line-0-" in sent_text
    assert "line-4-" in sent_text


@pytest.mark.asyncio
async def test_run_paper_digest_uses_llm_digest_when_topics_present():
    """run_paper_digest prefers the API digest when it returns topics."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = _make_config()

    with (
        patch.object(
            paper_digest,
            "_fetch_digest_from_api",
            AsyncMock(return_value={"topics": [{"name": "Agents"}], "total_papers": 2}),
        ) as fetch_digest,
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", AsyncMock()) as send_chunked,
        patch.object(paper_digest, "_simple_digest", AsyncMock()) as simple_digest,
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    fetch_digest.assert_awaited_once()
    send_chunked.assert_awaited_once()
    simple_digest.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_paper_digest_falls_back_to_simple_digest():
    """run_paper_digest falls back when the API returns no topic data."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = _make_config()

    with (
        patch.object(paper_digest, "_fetch_digest_from_api", AsyncMock(return_value=None)),
        patch.object(paper_digest, "_simple_digest", AsyncMock()) as simple_digest,
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    simple_digest.assert_awaited_once_with(db_pool, bot, config, 1234, db_user_id=None)


@pytest.mark.asyncio
async def test_simple_digest_query_includes_starred_boolean_or_clause():
    # Phase A migration (T3): paper_user_state now uses a state ENUM instead of
    # legacy boolean columns (archived, dismissed) and status text.
    # The digest must:
    #   - exclude papers in state 'trash' or 'done' via a top-level NOT EXISTS guard
    #   - include papers where starred=TRUE or state IN ('reading','done')
    #   - include papers with a positive pulse_thumbs recommendation_feedback entry
    #     (replaces the retired pulse_ratings table)
    db_pool = AsyncMock()
    db_pool.fetch.return_value = []
    bot = AsyncMock()

    await paper_digest._simple_digest(db_pool, bot, _make_config(), 1234, db_user_id=None)

    sql = db_pool.fetch.await_args.args[0]
    # pus2 alias used in the positive-inclusion subquery; pus used by NOT EXISTS guard
    assert "COALESCE(pus2.starred, FALSE) = TRUE" in sql
    assert "pus2.state = 'reading'" in sql
    # Top-level NOT EXISTS guard uses the new state ENUM (trash/done = excluded)
    assert "NOT EXISTS" in sql
    assert "pus.state IN ('trash', 'done')" in sql
    # recommendation_feedback replaces the retired pulse_ratings table
    assert "FROM recommendation_feedback rf" in sql
    assert "rf.signal = 'positive'" in sql
    assert "rf.source = 'pulse_thumbs'" in sql
    # Legacy columns/table must NOT appear
    assert "COALESCE(pus.archived, FALSE)" not in sql
    assert "COALESCE(pus.dismissed, FALSE)" not in sql
    assert "FROM pulse_ratings" not in sql


@pytest.mark.asyncio
async def test_simple_digest_db_user_id_default_none_matches_null_row():
    """db_user_id=None (default) causes IS NOT DISTINCT FROM $1 to match NULL rows.

    A paper archived by a NULL-user_id paper_user_state row must be excluded when
    _simple_digest is called with db_user_id=None (single-tenant mode).
    """
    db_pool = AsyncMock()
    # Simulate the query returning empty (archived row matched, paper excluded)
    db_pool.fetch.return_value = []
    bot = AsyncMock()

    await paper_digest._simple_digest(db_pool, bot, _make_config(), 1234, db_user_id=None)

    sql, bound_param = db_pool.fetch.await_args.args
    # IS NOT DISTINCT FROM must appear in all three subqueries
    assert sql.count("IS NOT DISTINCT FROM $1") == 3
    # The bound parameter must be None (matches NULL rows via IS NOT DISTINCT FROM)
    assert bound_param is None


@pytest.mark.asyncio
async def test_simple_digest_db_user_id_42_does_not_see_user_99_archived():
    """db_user_id=42 scopes queries so user 99's archived flag does not suppress paper.

    The SQL is parameterised with $1 = 42; a paper_user_state row with user_id=99
    and archived=TRUE must NOT cause the paper to be excluded from user 42's digest.
    We verify this by confirming the SQL is called with db_user_id=42, so the
    IS NOT DISTINCT FROM $1 predicate only matches user 42's rows.
    """
    db_pool = AsyncMock()
    # Simulate a paper making it through the filter (user 99's archived row ignored)
    db_pool.fetch.return_value = [
        {
            "id": 1,
            "title": "Visible Paper",
            "url": "https://example.com/p1",
            "published_date": None,
            "authors": None,
            "topic_name": "AI",
            "relevance_score": 0.9,
            "summary_brief": None,
            "confidence": None,
        }
    ]
    bot = AsyncMock()

    await paper_digest._simple_digest(db_pool, bot, _make_config(), 1234, db_user_id=42)

    _, bound_param = db_pool.fetch.await_args.args
    # The query must be scoped to user 42 — user 99's archived row won't match
    assert bound_param == 42


@pytest.mark.asyncio
async def test_simple_digest_passes_db_user_id_to_query():
    """_simple_digest passes db_user_id as the sole bound parameter to db_pool.fetch."""
    db_pool = AsyncMock()
    db_pool.fetch.return_value = []
    bot = AsyncMock()

    await paper_digest._simple_digest(db_pool, bot, _make_config(), 1234, db_user_id=42)

    call_args = db_pool.fetch.await_args.args
    # args[0] = SQL string, args[1] = db_user_id value
    assert len(call_args) == 2, "fetch must be called with exactly (sql, db_user_id)"
    assert call_args[1] == 42
