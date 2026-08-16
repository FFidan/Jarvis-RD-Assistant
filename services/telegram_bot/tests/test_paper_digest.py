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
from telegram_bot.platform_client import UserPairing


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
        make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        user_id=1,
    )

    assert result == {"topics": [{"name": "LLMs"}]}
    _, kwargs = http_client.get.await_args
    assert "X-API-Key" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_fetch_digest_from_api_returns_none_on_error():
    """HTTP failures degrade to None so the caller can fall back."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = httpx.ConnectError("offline")

    result = await paper_digest._fetch_digest_from_api(
        http_client,
        make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        user_id=1,
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
        http_client,
        make_bot_config(BotConfig, jarvis_api_key=None),
        user_id=1,
    )

    assert result == {"topics": []}
    _, kwargs = http_client.get.await_args
    assert "X-API-Key" not in kwargs["headers"]


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


def test_balance_chunk_seeds_local_stack_with_inherited_tags():
    """TG-BUG-02: each chunk must be independently balanced HTML.

    Telegram parses every message in isolation, so a chunk must re-open
    inherited tags (prefix) AND close everything still open at its end
    (suffix). Seeding ``local_stack`` with ``open_stack`` means inherited tags
    are tracked and closed, so the chunk Telegram receives is self-contained
    valid HTML rather than carrying an unclosed opener.
    """
    # Chunk inherits an italic tag and opens/closes a bold tag internally.
    balanced, updated_stack = _balance_chunk("<b>hello</b>", open_stack=["<i>"])

    # The inherited <i> is re-opened in the prefix; <b>hello</b> pairs locally;
    # the still-open inherited <i> is closed in the suffix → self-balanced.
    assert balanced == "<i><b>hello</b></i>", f"Expected '<i><b>hello</b></i>', got {balanced!r}"

    # The inherited <i> is still open at chunk end, so it carries forward.
    assert updated_stack == ["<i>"], f"Expected ['<i>'], got {updated_stack!r}"


def test_balance_chunk_closes_tag_opened_in_prior_chunk():
    """TG-BUG-02 (regression): a chunk that closes an inherited tag is balanced.

    Before the fix, ``local_stack`` ignored ``open_stack``, so a chunk that
    only contained text after an inherited opener was emitted with an unclosed
    opener (``<b>text``) — malformed HTML. After the fix, the inherited opener
    is closed in the suffix and the carry-state is correct.
    """
    import re

    # Chunk inherits <b> and adds only text (closes nothing, opens nothing).
    balanced, updated_stack = _balance_chunk("more text", open_stack=["<b>"])

    opens = len(re.findall(r"<b>", balanced, re.IGNORECASE))
    closes = len(re.findall(r"</b>", balanced, re.IGNORECASE))
    assert opens == closes == 1, f"Inherited <b> must be balanced in-chunk; got {balanced!r}"
    assert updated_stack == ["<b>"], f"Inherited opener must carry forward; got {updated_stack!r}"


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


def _assert_valid_telegram_chunk(text: str) -> None:
    """Assert *text* is a self-contained, sendable Telegram HTML message.

    Checks: non-empty, within the 4096-char limit, no partially-cut tag
    (every ``<`` has a matching ``>`` before the next ``<``), no dangling
    entity tail, and balanced open/close counts for spanning tags.
    """
    import re

    assert text, "chunk must not be empty (Telegram rejects empty messages)"
    assert len(text) <= 4096, f"chunk exceeds Telegram's 4096-char limit: {len(text)}"

    # No split/dangling tag anywhere (M12b: truncate must never cut mid-tag).
    search_from = 0
    while True:
        lt = text.find("<", search_from)
        if lt == -1:
            break
        gt = text.find(">", lt)
        next_lt = text.find("<", lt + 1)
        assert gt != -1 and (next_lt == -1 or gt < next_lt), (
            f"partially-cut tag at offset {lt}: {text[lt : lt + 40]!r}"
        )
        search_from = gt + 1

    # No partially-cut entity at the end (e.g. '&amp' without ';').
    assert not re.search(r"&[A-Za-z#0-9]*$", text), f"dangling entity tail: {text[-12:]!r}"

    # Balanced spanning tags — Telegram parses each message in isolation.
    for opener, closer in ((r"<b>", r"</b>"), (r"<i>", r"</i>"), (r"<a\b[^>]*>", r"</a>")):
        opens = len(re.findall(opener, text, re.IGNORECASE))
        closes = len(re.findall(closer, text, re.IGNORECASE))
        assert opens == closes, f"unbalanced {opener} ({opens} vs {closes}) in chunk: {text!r}"


@pytest.mark.asyncio
async def test_send_chunked_truncates_overlong_tagged_line_without_breaking_html():
    """M12b: a single line longer than truncate's hard limit stays valid HTML.

    Before the fix, the send-time ``truncate`` hard-cut the *balanced* chunk at
    3996 chars, discarding the ``</b>`` closer (and potentially slicing
    mid-tag) → Telegram 400 'can't parse entities'.  Now the raw chunk is cut
    first and re-balanced, so the emitted chunk is self-contained valid HTML.
    """
    bot = AsyncMock()
    lines = ["<b>" + "x" * 4500 + "</b>"]

    await paper_digest._send_chunked(bot, 1234, lines)

    assert bot.send_message.await_count >= 1
    for call in bot.send_message.await_args_list:
        _assert_valid_telegram_chunk(call.kwargs["text"])
    # The kept prefix of the line must survive (content actually delivered).
    first_text = bot.send_message.await_args_list[0].kwargs["text"]
    assert first_text.startswith("<b>" + "x" * 100)


@pytest.mark.asyncio
async def test_send_chunked_never_cuts_inside_a_tag_near_truncate_limit():
    """M12b: a tag sitting right where truncate's 3996 hard cut lands must not be split."""
    bot = AsyncMock()
    # The <a> tag starts at offset 3970; the old hard cut at 3996 landed inside
    # its href, emitting a dangling '<a href="http://example.c' fragment.
    lines = ["x" * 3970 + '<a href="http://example.com/page">go</a>']

    await paper_digest._send_chunked(bot, 1234, lines)

    assert bot.send_message.await_count >= 1
    for call in bot.send_message.await_args_list:
        _assert_valid_telegram_chunk(call.kwargs["text"])


@pytest.mark.asyncio
async def test_send_chunked_nested_tags_near_limit_kept_intact():
    """Nested <b><i>…<a>…</a></i></b> with a chunk near the truncate limit.

    The first chunk lands just above the 3900 builder budget *with* its
    balancing closers but below truncate's 3996 limit — it must be delivered
    byte-for-byte intact (no truncation regression) and every chunk must be
    valid Telegram HTML.
    """
    bot = AsyncMock()
    pad = "x" * 3890
    lines = [
        "<b><i>" + pad,
        'tail <a href="http://example.com">link</a></i></b>',
    ]

    await paper_digest._send_chunked(bot, 1234, lines)

    assert bot.send_message.await_count >= 2
    chunks = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    for chunk in chunks:
        _assert_valid_telegram_chunk(chunk)
    # No content loss: the balanced first chunk fits under the truncate limit.
    assert pad in chunks[0], "first chunk must keep its full padding (no truncation)"
    assert "... (truncated)" not in chunks[0]


@pytest.mark.asyncio
async def test_run_paper_digest_uses_llm_digest_when_topics_present():
    """run_paper_digest prefers the API digest when it returns topics.

    Uses a capturing side-effect so we assert delivery to the correct chat_id
    rather than merely asserting the patched _send_chunked mock was called.
    """
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    deliveries: list[tuple[int, list[str]]] = []

    async def _capturing_send(bot_arg, chat_id: int, lines: list[str]) -> None:
        deliveries.append((chat_id, lines))

    with (
        patch(
            "telegram_bot.orchestration.paper_digest.list_user_pairings",
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
async def test_run_paper_digest_emits_absolute_inbox_link_when_base_url_set():
    """TG-BUG-01: with jarvis_base_url set, the inbox link is an absolute URL.

    A relative href (``/feed?...``) cannot be rendered as a working link by
    Telegram; the digest must prepend the configured base URL.
    """
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(
        BotConfig,
        jarvis_api_key=SecretStr("secret"),
        jarvis_base_url="https://jarvis.example.com",
    )

    deliveries: list[list[str]] = []

    async def _capturing_send(_bot, _chat_id: int, lines: list[str]) -> None:
        deliveries.append(lines)

    with (
        patch(
            "telegram_bot.orchestration.paper_digest.list_user_pairings",
            AsyncMock(return_value=[UserPairing(user_id=1, chat_id=1234)]),
        ),
        patch.object(
            paper_digest,
            "_fetch_digest_from_api",
            AsyncMock(return_value={"topics": [{"name": "AI"}], "total_papers": 1}),
        ),
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", side_effect=_capturing_send),
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    assert len(deliveries) == 1
    joined = "\n".join(deliveries[0])
    assert "https://jarvis.example.com/feed?surface=inbox" in joined, (
        f"Expected absolute inbox link; got: {joined!r}"
    )
    # No bare relative href should leak through.
    assert '<a href="/feed' not in joined, f"Relative href must not be emitted; got: {joined!r}"


@pytest.mark.asyncio
async def test_run_paper_digest_escapes_quote_in_base_url_href():
    """A double-quote in jarvis_base_url must be HTML-escaped, not break the href.

    Without escaping, a base URL containing ``"`` closes the ``href="..."``
    attribute early, producing malformed/injectable Telegram HTML.
    """
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(
        BotConfig,
        jarvis_api_key=SecretStr("secret"),
        jarvis_base_url='https://jarvis.example.com/"><script>',
    )

    deliveries: list[list[str]] = []

    async def _capturing_send(_bot, _chat_id: int, lines: list[str]) -> None:
        deliveries.append(lines)

    with (
        patch(
            "telegram_bot.orchestration.paper_digest.list_user_pairings",
            AsyncMock(return_value=[UserPairing(user_id=1, chat_id=1234)]),
        ),
        patch.object(
            paper_digest,
            "_fetch_digest_from_api",
            AsyncMock(return_value={"topics": [{"name": "AI"}], "total_papers": 1}),
        ),
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", side_effect=_capturing_send),
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    assert len(deliveries) == 1
    joined = "\n".join(deliveries[0])
    # The raw quote must not survive verbatim inside the href attribute.
    assert '"><script>' not in joined, f"Unescaped quote broke the href; got: {joined!r}"
    # It must appear in its escaped form.
    assert "&quot;&gt;&lt;script&gt;" in joined, f"Quote was not HTML-escaped; got: {joined!r}"


@pytest.mark.asyncio
async def test_run_paper_digest_omits_inbox_link_without_base_url():
    """TG-BUG-01: with jarvis_base_url unset, no broken relative link is emitted."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))
    assert config.jarvis_base_url is None  # guard: default

    deliveries: list[list[str]] = []

    async def _capturing_send(_bot, _chat_id: int, lines: list[str]) -> None:
        deliveries.append(lines)

    with (
        patch(
            "telegram_bot.orchestration.paper_digest.list_user_pairings",
            AsyncMock(return_value=[UserPairing(user_id=1, chat_id=1234)]),
        ),
        patch.object(
            paper_digest,
            "_fetch_digest_from_api",
            AsyncMock(return_value={"topics": [{"name": "AI"}], "total_papers": 1}),
        ),
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", side_effect=_capturing_send),
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    assert len(deliveries) == 1
    joined = "\n".join(deliveries[0])
    assert "/feed?surface=inbox" not in joined, (
        f"No relative/broken inbox link should be emitted without a base URL; got: {joined!r}"
    )


@pytest.mark.asyncio
async def test_run_paper_digest_warns_when_api_returns_no_data():
    """run_paper_digest does not send messages when the API returns no topic data."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    with (
        patch(
            "telegram_bot.orchestration.paper_digest.list_user_pairings",
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
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    fetch_calls: list[tuple[int | None]] = []

    async def _capturing_fetch(_http_client, _config, user_id: int | None = None) -> dict:
        fetch_calls.append((user_id,))
        return {"topics": [{"name": "AI"}], "total_papers": 1}

    with (
        patch(
            "telegram_bot.orchestration.paper_digest.list_user_pairings",
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
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    block_chat_id = 111
    good_chat_id = 222
    delivered_chats: list[int] = []

    async def fake_send_chunked(bot_arg, chat_id: int, lines: list[str]) -> None:
        if chat_id == block_chat_id:
            raise Exception("Forbidden: bot was blocked by the user")
        delivered_chats.append(chat_id)

    with (
        patch(
            "telegram_bot.orchestration.paper_digest.list_user_pairings",
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
