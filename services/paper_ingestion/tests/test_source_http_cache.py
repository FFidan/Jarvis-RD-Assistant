"""Tests for CachingTransport in-memory source HTTP cache.

TDD — written before the implementation.  Uses a deterministic counting fake
inner transport (NOT respx — respx patches the default transport and would
bypass the wrapper under test).

Six cases:
1. Cache hit:  two identical GETs within TTL hit the inner transport once.
2. 429 NOT cached: 429 then 200; third request served from cache (inner=2).
3. Non-allowlisted host: always passes through, hits==0.
4. POST: never cached, inner called each time.
5. SOURCE_HTTP_CACHE_ENABLED=false: pure passthrough, hits==0.
6. Binary content-type (PDF): never cached even for an allowlisted host.
"""

from __future__ import annotations

import logging

import httpx
from jarvis_common.cached_transport import CachingTransport

_S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search?query=test"
_OTHER_URL = "https://example.com/x"
_NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=crispr"


class _CountingInner(httpx.AsyncBaseTransport):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        item = self.responses.pop(0) if self.responses else (200, b"{}")
        status, body, *rest = item
        headers = rest[0] if rest else None
        return httpx.Response(status, content=body, headers=headers, request=request)

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


# ---------------------------------------------------------------------------
# 6. Binary content-type → never cached (PDF stream-to-disk stays intact)
# ---------------------------------------------------------------------------


async def test_binary_content_type_not_cached():
    """A 200 with a binary Content-Type on an allowlisted host is never cached."""
    pdf_hdr = {"content-type": "application/pdf"}
    inner = _CountingInner([(200, b"%PDF-1.7", pdf_hdr), (200, b"%PDF-1.7", pdf_hdr)])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_S2_URL)
        r2 = await client.get(_S2_URL)

    assert inner.calls == 2
    assert transport.hits == 0
    assert r1.status_code == 200
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# 7. PI-D — api_key query param is excluded from the cache key
# ---------------------------------------------------------------------------


async def test_secret_query_param_excluded_from_cache_key():
    """Two GETs differing only by ``api_key`` collapse to one cached entry and
    the secret never appears in a stored cache key."""
    inner = _CountingInner([(200, b'{"ids":[1]}'), (200, b'{"ids":[2]}')])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(f"{_NCBI}&api_key=SECRET-AAA")
        r2 = await client.get(f"{_NCBI}&api_key=SECRET-BBB")

    assert inner.calls == 1
    assert transport.hits == 1
    assert r1.content == r2.content
    assert all("SECRET-" not in stored_key for stored_key in transport._store)


# ---------------------------------------------------------------------------
# 8. PI-D — the secret is not emitted to the debug log
# ---------------------------------------------------------------------------


async def test_secret_not_written_to_debug_log(caplog):
    """DEBUG hit/miss logging must use the sanitized key, not the raw URL."""
    caplog.set_level(logging.DEBUG, logger="jarvis_common.cached_transport")
    inner = _CountingInner([(200, b"{}"), (200, b"{}")])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(f"{_NCBI}&api_key=TOPSECRET")  # MISS — logs
        await client.get(f"{_NCBI}&api_key=TOPSECRET")  # HIT — logs

    assert "TOPSECRET" not in caplog.text
    assert "esearch.fcgi" in caplog.text  # sanitized key still logged


# ---------------------------------------------------------------------------
# 9. PI-E — hop-by-hop / framing headers are stripped before caching
# ---------------------------------------------------------------------------


async def test_hop_by_hop_headers_stripped():
    """Buffered cache entries must not carry upstream connection/transfer
    framing headers — on the freshly-stored response or the cache hit."""
    upstream = {
        "transfer-encoding": "chunked",
        "connection": "keep-alive",
        "content-length": "99999",
        "content-type": "application/json",
        "cache-control": "max-age=60",
    }
    inner = _CountingInner([(200, b'{"ok":true}', upstream)])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_S2_URL)  # fresh store
        r2 = await client.get(_S2_URL)  # cache hit

    for r in (r1, r2):
        assert "transfer-encoding" not in r.headers
        assert "connection" not in r.headers
        assert r.headers["content-type"] == "application/json"
        assert r.headers["cache-control"] == "max-age=60"
        assert r.headers["content-length"] == str(len(b'{"ok":true}'))
    assert r1.content == r2.content == b'{"ok":true}'
    assert transport.hits == 1
