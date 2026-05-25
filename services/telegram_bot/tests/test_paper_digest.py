"""Unit tests for the weekly paper digest workflow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jarvis_common.testing import make_bot_config
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.orchestration import paper_digest
from telegram_bot.orchestration.paper_digest import _balance_chunk


@pytest.mark.asyncio
async def test_fetch_digest_from_api_returns_payload_and_auth_header():
    """The digest fetch helper returns parsed JSON and includes the API key header."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"topics": [{"name": "LLMs"}]}
    http_client.get.return_value = response

    result = await paper_digest._fetch_digest_from_api(
        http_client,
        make_bot_config(BotConfig, telegram_chat_id=1234, jarvis_api_key=SecretStr("secret")),
    )

    assert result == {"topics": [{"name": "LLMs"}]}
    _, kwargs = http_client.get.await_args
    assert kwargs["headers"]["X-API-Key"] == "secret"


@pytest.mark.asyncio
async def test_fetch_digest_from_api_returns_none_on_error():
    """HTTP failures degrade to None so the caller can fall back."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = httpx.ConnectError("offline")

    result = await paper_digest._fetch_digest_from_api(
        http_client,
        make_bot_config(BotConfig, telegram_chat_id=1234, jarvis_api_key=SecretStr("secret")),
    )

    assert result is None


@pytest.mark.asyncio
async def test_fetch_digest_from_api_omits_auth_header_without_api_key():
    """Empty bot API keys should not emit an auth header."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"topics": []}
    http_client.get.return_value = response

    result = await paper_digest._fetch_digest_from_api(
        http_client, make_bot_config(BotConfig, telegram_chat_id=1234, jarvis_api_key=None)
    )

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
    # §D8-02: text payload must include both lines (not just a vacuous send check)
    text = kwargs["text"]
    assert "line one" in text, f"Expected 'line one' in sent text; got: {text!r}"
    assert "line two" in text, f"Expected 'line two' in sent text; got: {text!r}"


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


def test_balance_chunk_no_double_count_inherited_tags():
    """Inherited tags must not be closed in the suffix, preventing malformed HTML.

    Regression test for HIGH-TG-02: when a chunk inherits tags from the previous
    chunk, those inherited tags are re-opened in the prefix but must NOT be closed
    in the suffix. This prevents the double-close bug where tags get closed at the
    end of one chunk and then re-opened/re-closed at the start of the next.
    """
    # Chunk that inherits an italic tag but doesn't open/close any new tags
    balanced, updated_stack = _balance_chunk("<b>hello</b>", open_stack=["<i>"])

    # The inherited <i> is re-opened in the prefix, the chunk is added.
    # The suffix is empty because no new tags are open at chunk end.
    # Result: italic stays open, bold is properly paired.
    assert balanced == "<i><b>hello</b>", f"Expected '<i><b>hello</b>', got {balanced!r}"

    # No new tags opened in this chunk, so the updated stack is empty.
    # The inherited italic is NOT returned here; it stays active due to the
    # prefix mechanism and will be re-specified in the next chunk's open_stack.
    assert updated_stack == [], f"Expected [], got {updated_stack!r}"


@pytest.mark.asyncio
async def test_send_chunked_balances_tags_across_boundary():
    """<b>...</b> straddling the 3900-char boundary must be closed/reopened."""
    bot = AsyncMock()
    # Build a message where <b>…</b> straddles the split point.
    # "pad" fills the first chunk to just below 3900 chars; "tail" lands in
    # the second chunk.  The tag opens before the boundary and closes after.
    pad = "x" * 3890
    lines = [pad, "<b>foo bar baz</b>"]

    await paper_digest._send_chunked(bot, 1234, lines)

    assert bot.send_message.await_count >= 2
    calls = bot.send_message.await_args_list

    # Every chunk must have balanced tags (equal opens and closes for <b>).
    import re

    for call in calls:
        text = call.kwargs["text"]
        opens = len(re.findall(r"<b>", text, re.IGNORECASE))
        closes = len(re.findall(r"</b>", text, re.IGNORECASE))
        assert opens == closes, f"Unbalanced <b> in chunk: {text!r}"


@pytest.mark.asyncio
async def test_send_chunked_handles_nested_tags():
    """Nested <b><a>…</a></b> straddling a boundary must be balanced in both chunks."""
    bot = AsyncMock()
    pad = "x" * 3888
    # The nested tags open before the split and close after it.
    lines = [pad, '<b><a href="http://example.com">link text</a></b>']

    await paper_digest._send_chunked(bot, 1234, lines)

    assert bot.send_message.await_count >= 2
    calls = bot.send_message.await_args_list

    import re

    for call in calls:
        text = call.kwargs["text"]
        b_opens = len(re.findall(r"<b>", text, re.IGNORECASE))
        b_closes = len(re.findall(r"</b>", text, re.IGNORECASE))
        a_opens = len(re.findall(r"<a\b[^>]*>", text, re.IGNORECASE))
        a_closes = len(re.findall(r"</a>", text, re.IGNORECASE))
        assert b_opens == b_closes, f"Unbalanced <b> in chunk: {text!r}"
        assert a_opens == a_closes, f"Unbalanced <a> in chunk: {text!r}"


@pytest.mark.asyncio
async def test_run_paper_digest_uses_llm_digest_when_topics_present():
    """run_paper_digest prefers the API digest when it returns topics.

    Uses a capturing side-effect so we assert delivery to the correct chat_id
    rather than merely asserting the patched _send_chunked mock was called.
    """
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(BotConfig, telegram_chat_id=1234, jarvis_api_key=SecretStr("secret"))

    from telegram_bot.owner import UserPairing

    deliveries: list[tuple[int, list[str]]] = []

    async def _capturing_send(bot_arg, chat_id: int, lines: list[str]) -> None:
        deliveries.append((chat_id, lines))

    with (
        patch(
            "telegram_bot.owner.list_user_pairings",
            AsyncMock(return_value=[UserPairing(user_id=1, chat_id=1234)]),
        ),
        patch.object(
            paper_digest,
            "_fetch_digest_from_api",
            AsyncMock(return_value={"topics": [{"name": "Agents"}], "total_papers": 2}),
        ) as fetch_digest,
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", side_effect=_capturing_send),
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    fetch_digest.assert_awaited_once()
    assert len(deliveries) == 1, f"Expected 1 delivery, got {len(deliveries)}"
    delivered_chat_id, delivered_lines = deliveries[0]
    assert delivered_chat_id == 1234, (
        f"Digest delivered to wrong chat_id: expected 1234, got {delivered_chat_id}"
    )
    assert any("digest line" in line for line in delivered_lines), (
        f"Expected formatted digest content in delivered lines; got: {delivered_lines}"
    )


@pytest.mark.asyncio
async def test_run_paper_digest_warns_when_api_returns_no_data():
    """run_paper_digest does not send messages when the API returns no topic data."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(BotConfig, telegram_chat_id=1234, jarvis_api_key=SecretStr("secret"))

    from telegram_bot.owner import UserPairing

    with (
        patch(
            "telegram_bot.owner.list_user_pairings",
            AsyncMock(return_value=[UserPairing(user_id=1, chat_id=1234)]),
        ),
        patch.object(paper_digest, "_fetch_digest_from_api", AsyncMock(return_value=None)),
        patch.object(paper_digest, "_send_chunked", AsyncMock()) as send_chunked,
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    # No message should be sent when API returns no data
    send_chunked.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_digest_per_pairing_user_scope():
    """Each paired user gets their own API call with X-Owner-User-Id scoping (N1 fix).

    Two pairings → two _fetch_digest_from_api calls with different user_ids,
    so the backend scopes the digest to each user's data independently.
    """
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(BotConfig, telegram_chat_id=1234, jarvis_api_key=SecretStr("secret"))

    from telegram_bot.owner import UserPairing

    fetch_calls: list[tuple[int | None]] = []

    async def _capturing_fetch(_http_client, _config, user_id: int | None = None) -> dict:
        fetch_calls.append((user_id,))
        return {"topics": [{"name": "AI"}], "total_papers": 1}

    with (
        patch(
            "telegram_bot.owner.list_user_pairings",
            AsyncMock(
                return_value=[
                    UserPairing(user_id=10, chat_id=1000),
                    UserPairing(user_id=20, chat_id=2000),
                ]
            ),
        ),
        patch.object(paper_digest, "_fetch_digest_from_api", side_effect=_capturing_fetch),
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", AsyncMock()),
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    # API must be called once per pairing — not once shared across all users
    assert len(fetch_calls) == 2, f"Expected 2 API calls, got {len(fetch_calls)}"

    # Each call must carry the correct user_id for its pairing
    user_ids_sent = {call[0] for call in fetch_calls}
    assert user_ids_sent == {10, 20}, f"Expected user_ids {{10, 20}}, got {user_ids_sent}"


@pytest.mark.asyncio
async def test_digest_continues_after_blocked_user():
    """A 403 Telegram error for one user must not prevent digest delivery to others."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(BotConfig, telegram_chat_id=1234, jarvis_api_key=SecretStr("secret"))

    from telegram_bot.owner import UserPairing

    block_chat_id = 111
    good_chat_id = 222
    delivered_chats: list[int] = []

    async def fake_send_chunked(bot_arg, chat_id: int, lines: list[str]) -> None:
        if chat_id == block_chat_id:
            raise Exception("Forbidden: bot was blocked by the user")
        delivered_chats.append(chat_id)

    with (
        patch(
            "telegram_bot.owner.list_user_pairings",
            AsyncMock(
                return_value=[
                    UserPairing(user_id=1, chat_id=block_chat_id),
                    UserPairing(user_id=2, chat_id=good_chat_id),
                ]
            ),
        ),
        patch.object(
            paper_digest,
            "_fetch_digest_from_api",
            AsyncMock(return_value={"topics": [{"name": "AI"}], "total_papers": 1}),
        ),
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", side_effect=fake_send_chunked),
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    assert good_chat_id in delivered_chats, (
        "Good user must still get digest even after a blocked user raises a 403"
    )
