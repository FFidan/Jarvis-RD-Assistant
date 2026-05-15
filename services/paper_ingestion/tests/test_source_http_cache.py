"""Tests for CachingTransport in-memory source HTTP cache.

TDD — written before the implementation.  Uses a deterministic counting fake
inner transport (NOT respx — respx patches the default transport and would
bypass the wrapper under test).

Five cases:
1. Cache hit:  two identical GETs within TTL hit the inner transport once.
2. 429 NOT cached: 429 then 200; third request served from cache (inner=2).
3. Non-allowlisted host: always passes through, hits==0.
4. POST: never cached, inner called each time.
5. SOURCE_HTTP_CACHE_ENABLED=false: pure passthrough, hits==0.
"""

from __future__ import annotations

import httpx
from jarvis_common.cached_transport import CachingTransport

_S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search?query=test"
_OTHER_URL = "https://example.com/x"


class _CountingInner(httpx.AsyncBaseTransport):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        status, body = self.responses.pop(0) if self.responses else (200, b"{}")
        return httpx.Response(status, content=body, request=request)

    async def aclose(self):  # noqa: D401
        pass


# ---------------------------------------------------------------------------
# 1. Cache hit within TTL
# ---------------------------------------------------------------------------


async def test_cache_hit_within_ttl():
    """Two identical GETs within TTL: inner called once, hits==1, same content."""
    inner = _CountingInner([(200, b'{"data":[]}'), (200, b'{"data":["other"]}')])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_S2_URL)
        r2 = await client.get(_S2_URL)

    assert inner.calls == 1
    assert transport.hits == 1
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.content == r2.content


# ---------------------------------------------------------------------------
# 2. 429 NOT cached
# ---------------------------------------------------------------------------


async def test_429_not_cached():
    """429 is not stored; subsequent GET of same URL can hit the 200 cache."""
    inner = _CountingInner([(429, b""), (200, b'{"result":"ok"}')])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_S2_URL)  # 429 — NOT cached
        r2 = await client.get(_S2_URL)  # 200 — cached
        r3 = await client.get(_S2_URL)  # served from cache

    assert inner.calls == 2
    assert r1.status_code == 429
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert r2.content == r3.content


# ---------------------------------------------------------------------------
# 3. Non-allowlisted host → always passes through
# ---------------------------------------------------------------------------


async def test_non_allowlisted_host_passthrough():
    """GETs to hosts not in _SOURCE_HOSTS are never cached."""
    inner = _CountingInner([(200, b"a"), (200, b"b"), (200, b"c")])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(_OTHER_URL)
        await client.get(_OTHER_URL)
        await client.get(_OTHER_URL)

    assert inner.calls == 3
    assert transport.hits == 0


# ---------------------------------------------------------------------------
# 4. POST → never cached
# ---------------------------------------------------------------------------


async def test_post_never_cached():
    """POST requests bypass the cache regardless of host."""
    inner = _CountingInner([(200, b"1"), (200, b"2")])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.post(_S2_URL, content=b"{}")
        r2 = await client.post(_S2_URL, content=b"{}")

    assert inner.calls == 2
    assert transport.hits == 0
    assert r1.content != r2.content  # distinct responses returned


# ---------------------------------------------------------------------------
# 5. SOURCE_HTTP_CACHE_ENABLED=false → pure passthrough
# ---------------------------------------------------------------------------


async def test_disabled_via_env_is_pure_passthrough(monkeypatch):
    """Setting SOURCE_HTTP_CACHE_ENABLED=false disables caching entirely."""
    monkeypatch.setenv("SOURCE_HTTP_CACHE_ENABLED", "false")

    inner = _CountingInner([(200, b"x"), (200, b"y")])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(_S2_URL)
        await client.get(_S2_URL)

    assert inner.calls == 2
    assert transport.hits == 0
