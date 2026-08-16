"""Scoped Telegram pairing and downstream assertion contracts."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from jarvis_common.testing import make_bot_config, make_ptb_context, make_telegram_update
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import auth_check
from telegram_bot.service_auth import TelegramBackendAuth


def _config() -> BotConfig:
    return make_bot_config(
        BotConfig,
        platform_api_url="http://platform:8003",
        paper_ingestion_url="http://research:8000",
        learning_engine_url="http://learning:8001",
    )


def _client(handler: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_auth_check_accepts_platform_pairing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pairings/999")
        return httpx.Response(
            200,
            json={
                "user_id": 7,
                "chat_id": 999,
                "telegram_username": None,
                "paired_at": None,
            },
        )

    async with _client(handler) as platform_client:
        result = await auth_check(
            make_telegram_update(chat_id=999),
            _config(),
            platform_client,
        )

    assert result == (True, 7)


@pytest.mark.asyncio
async def test_auth_check_rejects_unpaired_chat() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _client(handler) as platform_client:
        result = await auth_check(
            make_telegram_update(chat_id=999),
            _config(),
            platform_client,
        )

    assert result == (False, None)


@pytest.mark.asyncio
async def test_auth_check_platform_outage_denies() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with _client(handler) as platform_client:
        result = await auth_check(
            make_telegram_update(chat_id=999),
            _config(),
            platform_client,
        )

    assert result == (False, None)


@pytest.mark.asyncio
async def test_auth_check_denies_group_without_platform_lookup() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with _client(handler) as platform_client:
        result = await auth_check(
            make_telegram_update(chat_id=-1001, chat_type="group"),
            _config(),
            platform_client,
        )

    assert result == (False, None)
    assert calls == 0


@pytest.mark.asyncio
async def test_auth_required_unpaired_skips_handler() -> None:
    called = False

    @auth_required
    async def handler(update, context) -> None:
        nonlocal called
        called = True

    async def platform_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _client(platform_handler) as platform_client:
        context = make_ptb_context(platform_client, _config())
        update = make_telegram_update(chat_id=4242, text="/help")
        await handler(update, context)

    assert called is False
    assert "/pair" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_auth_required_stashes_platform_user() -> None:
    seen: list[int | None] = []

    @auth_required
    async def handler(update, context) -> None:
        seen.append(context.user_data.get("jarvis_user_id"))

    async def platform_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"user_id": 99, "chat_id": 4242, "telegram_username": None, "paired_at": None},
        )

    async with _client(platform_handler) as platform_client:
        context = make_ptb_context(platform_client, _config())
        await handler(make_telegram_update(chat_id=4242, text="/help"), context)

    assert seen == [99]


@pytest.mark.asyncio
async def test_backend_auth_exchanges_and_strips_local_owner_marker() -> None:
    platform_requests: list[dict[str, object]] = []
    backend_headers: httpx.Headers | None = None

    async def platform_handler(request: httpx.Request) -> httpx.Response:
        platform_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"assertion": "signed-assertion"})

    async def backend_handler(request: httpx.Request) -> httpx.Response:
        nonlocal backend_headers
        backend_headers = request.headers
        return httpx.Response(200, json={"ok": True})

    config = _config()
    async with (
        _client(platform_handler) as platform_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(backend_handler),
            auth=TelegramBackendAuth(config, platform_client),
        ) as backend_client,
    ):
        response = await backend_client.get(
            "http://research:8000/api/papers/42",
            headers={"X-Owner-User-Id": "7", "X-API-Key": "must-not-leave"},
        )

    assert response.status_code == 200
    assert platform_requests[0]["audience"] == "research"
    assert platform_requests[0]["user_id"] == 7
    assert backend_headers is not None
    assert backend_headers["X-Jarvis-Identity"] == "signed-assertion"
    assert "X-Request-Id" in backend_headers
    assert "X-Owner-User-Id" not in backend_headers
    assert "X-API-Key" not in backend_headers


@pytest.mark.asyncio
async def test_backend_auth_denies_unmanifested_route_before_exchange() -> None:
    exchanges = 0

    async def platform_handler(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges
        exchanges += 1
        return httpx.Response(200, json={"assertion": "unexpected"})

    config = _config()
    async with (
        _client(platform_handler) as platform_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            auth=TelegramBackendAuth(config, platform_client),
        ) as backend_client,
    ):
        with pytest.raises(RuntimeError, match="not allowlisted"):
            await backend_client.delete(
                "http://research:8000/api/admin/users/7",
                headers={"X-Owner-User-Id": "7"},
            )

    assert exchanges == 0


@pytest.mark.asyncio
async def test_backend_auth_requires_paired_user_marker() -> None:
    config = _config()
    async with (
        _client(lambda request: _response(200)) as platform_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            auth=TelegramBackendAuth(config, platform_client),
        ) as backend_client,
    ):
        with pytest.raises(RuntimeError, match="paired user"):
            await backend_client.get("http://research:8000/api/papers/42")


async def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code)
