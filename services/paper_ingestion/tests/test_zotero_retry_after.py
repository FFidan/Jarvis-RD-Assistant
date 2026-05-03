"""Tests for Zotero 429 / Retry-After handling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from paper_ingestion.integrations.zotero_client import (
    _MAX_RETRY_AFTER_SECONDS,
    ZoteroClient,
    _parse_retry_after,
    _zotero_request_with_retry,
)

# ---------------------------------------------------------------------------
# _parse_retry_after
# ---------------------------------------------------------------------------


def test_parse_retry_after_delta_seconds():
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("0") == 0.0


def test_parse_retry_after_clamps_excessive_values():
    """A 1-day value must be clamped to _MAX_RETRY_AFTER_SECONDS."""
    assert _parse_retry_after("86400") == _MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_negative_returns_none():
    assert _parse_retry_after("-1") is None


def test_parse_retry_after_garbage_returns_none():
    assert _parse_retry_after("not-a-number-or-date") is None


def test_parse_retry_after_none_input():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None


def test_parse_retry_after_http_date_in_future_clamped():
    """An HTTP-date far in the future is still clamped to the cap."""
    delay = _parse_retry_after("Wed, 21 Oct 9999 07:28:00 GMT")
    assert delay is not None
    assert delay <= _MAX_RETRY_AFTER_SECONDS


# ---------------------------------------------------------------------------
# _zotero_request_with_retry
# ---------------------------------------------------------------------------


def _resp(status: int, headers: dict | None = None, body=None) -> httpx.Response:
    """Build a synthetic httpx.Response with the given status / headers."""
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        content=b"" if body is None else body,
        request=httpx.Request("GET", "https://api.zotero.org/test"),
    )


@pytest.mark.asyncio
async def test_retry_after_429_then_success(monkeypatch):
    """A 429 with a small Retry-After triggers exactly one retry that succeeds."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("paper_ingestion.integrations.zotero_client.asyncio.sleep", fake_sleep)

    http = AsyncMock(spec=httpx.AsyncClient)
    http.request = AsyncMock(
        side_effect=[
            _resp(429, headers={"Retry-After": "1"}),
            _resp(200, body=b"[]"),
        ]
    )

    resp = await _zotero_request_with_retry("GET", http, "https://api.zotero.org/test")
    assert resp.status_code == 200
    assert http.request.await_count == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_retry_after_429_exhausted(monkeypatch):
    """If the retry also returns 429, the helper surfaces it without a 3rd call."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("paper_ingestion.integrations.zotero_client.asyncio.sleep", fake_sleep)

    http = AsyncMock(spec=httpx.AsyncClient)
    http.request = AsyncMock(
        side_effect=[
            _resp(429, headers={"Retry-After": "1"}),
            _resp(429, headers={"Retry-After": "1"}),
        ]
    )

    resp = await _zotero_request_with_retry("GET", http, "https://api.zotero.org/test")
    assert resp.status_code == 429
    assert http.request.await_count == 2  # one initial + one retry, no third
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_retry_skipped_when_retry_after_missing():
    """A 429 without Retry-After must NOT retry (avoids hot-loop on bad upstreams)."""
    http = AsyncMock(spec=httpx.AsyncClient)
    http.request = AsyncMock(return_value=_resp(429))

    resp = await _zotero_request_with_retry("GET", http, "https://api.zotero.org/test")
    assert resp.status_code == 429
    assert http.request.await_count == 1


@pytest.mark.asyncio
async def test_non_429_passthrough():
    http = AsyncMock(spec=httpx.AsyncClient)
    http.request = AsyncMock(return_value=_resp(200, body=b"ok"))
    resp = await _zotero_request_with_retry("GET", http, "https://api.zotero.org/test")
    assert resp.status_code == 200
    assert http.request.await_count == 1


@pytest.mark.asyncio
async def test_search_by_doi_uses_retry_helper(monkeypatch):
    """Smoke test: the public ZoteroClient API runs through the retry helper."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("paper_ingestion.integrations.zotero_client.asyncio.sleep", fake_sleep)

    http = AsyncMock(spec=httpx.AsyncClient)
    http.request = AsyncMock(
        side_effect=[
            _resp(429, headers={"Retry-After": "1"}),
            _resp(200, body=b'[{"key":"ABC123"}]'),
        ]
    )
    client = ZoteroClient(api_key="k", user_id="42", http_client=http)
    result = await client.search_by_doi("10.1234/test")
    assert result == {"key": "ABC123"}
    assert http.request.await_count == 2
    assert sleeps == [1.0]
