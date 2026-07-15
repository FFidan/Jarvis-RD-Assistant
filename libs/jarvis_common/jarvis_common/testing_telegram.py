"""Shared Telegram-bot test infrastructure: PTB Update/BotConfig factories + async-CM stubs.

Telegram-focused helpers extracted from ``jarvis_common.testing``:

7. ``make_telegram_update`` + ``make_bot_config`` (PTB-level factories used by D9-04)
8. ``FakeAcquireCM`` + ``FakeTxnCM`` (asyncpg async-CM stubs used by telegram pairing tests, D8-04)
"""

from __future__ import annotations

__all__ = [
    "FakeAcquireCM",
    "FakeTxnCM",
    "make_telegram_update",
    "make_bot_config",
    "make_http_response",
]

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

# ---------------------------------------------------------------------------
# FakeAcquireCM / FakeTxnCM  (telegram_bot pairing tests, D8-04)
# ---------------------------------------------------------------------------


class FakeAcquireCM:
    """Async context manager returned by ``pool.acquire()`` in telegram tests."""

    def __init__(self, conn: Any) -> None:
        """Hold the connection that will be yielded by the async context manager."""
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeTxnCM:
    """Async context manager returned by ``conn.transaction()`` in telegram tests."""

    async def __aenter__(self) -> FakeTxnCM:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Telegram helpers  (D9-04 — _make_update defined 6× across telegram_bot)
# ---------------------------------------------------------------------------


def make_telegram_update(
    chat_id: int = 42,
    *,
    text: str | None = None,
    user_id: int | None = None,
    username: str | None = "testuser",
    chat_type: str = "private",
) -> MagicMock:
    """Build a minimal PTB ``Update``-like MagicMock.

    Superset of all 6 local ``_make_update`` variants found in
    ``telegram_bot/tests/``:

    - ``test_pairing.py``         — chat_id, text, username
    - ``test_pairing_command.py`` — chat_id, text
    - ``test_pairing_takeover.py``— chat_id
    - ``test_rate_limit.py``      — chat_id
    - ``test_auth.py``            — chat_id
    - ``test_dispatcher_correlation.py`` — chat_id

    ``user_id`` wires ``update.effective_user.id`` for handlers that inspect
    the PTB user object (not used in current tests but anticipated by D9-04).
    ``username`` wires ``update.effective_chat.username``.
    ``chat_type`` wires ``update.effective_chat.type`` (defaults to ``"private"``
    since these fakes simulate 1:1 DMs; pass ``"group"``/``"supergroup"`` to
    exercise the non-private auth guard).
    """
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_chat.username = username
    update.message = MagicMock()
    if text is not None:
        update.message.text = text
    update.message.reply_text = AsyncMock()
    if user_id is not None:
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
    return update


def make_bot_config(bot_config_cls: Any, **overrides: Any) -> Any:
    """Build a minimal ``BotConfig`` for telegram_bot unit tests.

    ``bot_config_cls`` is the ``BotConfig`` class (or a compatible class) from
    the calling service — injected by the caller so that this library function
    does not import from any service package.

    Defaults match the canonical ``_make_config`` in ``test_pairing.py``;
    pass ``**overrides`` to change individual fields (e.g.
    ``telegram_chat_id=None`` for pairing-flow tests).
    """
    from pydantic import SecretStr

    defaults: dict[str, Any] = dict(
        telegram_token="test-token",
        telegram_chat_id=777,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key=SecretStr("test-key"),
    )
    defaults.update(overrides)
    return bot_config_cls(**defaults)


# ---------------------------------------------------------------------------
# HTTP response mock helper  (services_client tests)
# ---------------------------------------------------------------------------


def make_http_response(json_data: Any, *, status: int = 200) -> MagicMock:
    """Build a minimal httpx Response-shaped mock.

    Parameters
    ----------
    json_data:
        The value returned by ``.json()``.
    status:
        HTTP status code stored in ``.status_code``.  When *status* >= 400,
        ``.raise_for_status()`` raises ``httpx.HTTPStatusError``; otherwise it
        is a no-op.

    Returns
    -------
    MagicMock
        A mock that satisfies the ``raise_for_status → json()`` contract used
        by ``services_client`` and other bot REST helpers.
    """
    resp = MagicMock()
    resp.status_code = status

    if status >= 400:
        # Build a minimal real HTTPStatusError so isinstance checks work.
        _request = httpx.Request("GET", "http://test")
        _response = httpx.Response(status_code=status, request=_request)
        _exc = httpx.HTTPStatusError(
            message=f"HTTP {status}",
            request=_request,
            response=_response,
        )
        resp.raise_for_status = MagicMock(side_effect=_exc)
    else:
        resp.raise_for_status = MagicMock()  # no-op

    resp.json = MagicMock(return_value=json_data)
    return resp
