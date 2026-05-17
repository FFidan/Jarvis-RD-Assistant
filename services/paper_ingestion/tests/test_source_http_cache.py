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

Plus PI-D/PI-E lock-in (tests 7-9) and SEC-3 regression (tests 14-15).
"""

from __future__ import annotations

import logging

import httpx
from jarvis_common.cached_transport import CachingTransport

_S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search?query=test"
_OTHER_URL = "https://example.com/x"
_NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=crispr"
_ARXIV = "https://export.arxiv.org/api/query?search_query=all:neural+ode"

_ATOM_OK = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>'


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
    # NCBI E-utilities returns XML (retmode=xml); use a well-formed body so the
    # B3 body guard caches it — this test is about the credential-stripped key.
    inner = _CountingInner(
        [(200, b"<eSearchResult><IdList/></eSearchResult>"), (200, b"<eSearchResult/>")]
    )
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
    xml = b"<eSearchResult/>"  # NCBI returns XML; well-formed so it caches
    inner = _CountingInner([(200, xml), (200, xml)])
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


# ---------------------------------------------------------------------------
# 10. B3 — malformed XML from an XML source host is NOT cached
# ---------------------------------------------------------------------------


async def test_malformed_xml_not_cached():
    """A transient broken arXiv body must not poison the cache: the inner is
    re-hit on the next request and the bad body is still returned to caller."""
    bad = b"<feed><entry></feed>"  # unbalanced tags — not well-formed
    inner = _CountingInner([(200, bad), (200, _ATOM_OK)])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_ARXIV)  # malformed — NOT cached
        r2 = await client.get(_ARXIV)  # inner hit again, now good — cached
        r3 = await client.get(_ARXIV)  # served from cache

    assert inner.calls == 2
    assert transport.hits == 1
    assert r1.status_code == 200
    assert r1.content == bad  # caller still gets the (bad) body to retry/handle
    assert r2.content == r3.content == _ATOM_OK


# ---------------------------------------------------------------------------
# 11. B3 — well-formed Atom from arXiv IS cached
# ---------------------------------------------------------------------------


async def test_wellformed_atom_is_cached():
    """A valid Atom feed from export.arxiv.org is cached like any 200 body."""
    inner = _CountingInner([(200, _ATOM_OK), (200, b"<feed>changed</feed>")])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_ARXIV)
        r2 = await client.get(_ARXIV)

    assert inner.calls == 1
    assert transport.hits == 1
    assert r1.content == r2.content == _ATOM_OK


# ---------------------------------------------------------------------------
# 12. B3 — empty 200 body is never cached (JSON host)
# ---------------------------------------------------------------------------


async def test_empty_body_not_cached():
    """An empty 200 body on an allowlisted JSON host is not cached."""
    inner = _CountingInner([(200, b""), (200, b'{"data":[]}')])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_S2_URL)  # empty — NOT cached
        r2 = await client.get(_S2_URL)  # inner hit again, now real — cached
        r3 = await client.get(_S2_URL)  # served from cache

    assert inner.calls == 2
    assert transport.hits == 1
    assert r1.status_code == 200
    assert r1.content == b""
    assert r2.content == r3.content == b'{"data":[]}'


# ---------------------------------------------------------------------------
# 13. B3 — empty 200 body on an XML host is also not cached
# ---------------------------------------------------------------------------


async def test_empty_body_xml_host_not_cached():
    """An empty 200 from arXiv is treated as no usable body, not as XML."""
    inner = _CountingInner([(200, b""), (200, _ATOM_OK)])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_ARXIV)
        r2 = await client.get(_ARXIV)
        r3 = await client.get(_ARXIV)

    assert inner.calls == 2
    assert transport.hits == 1
    assert r1.content == b""
    assert r2.content == r3.content == _ATOM_OK


# ---------------------------------------------------------------------------
# 14. SEC-3 — entity-expansion payload is rejected by the XML gate
# ---------------------------------------------------------------------------

# A modest billion-laughs variant: 3 expansion levels, stays CPU/memory safe
# in any parser but any stdlib-based expander would expand the entities while
# lxml with resolve_entities=False refuses to parse it at all.
_BILLION_LAUGHS = b"""\
<?xml version="1.0"?>
<!DOCTYPE feed [
  <!ENTITY a "AAAA">
  <!ENTITY b "&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;">
]>
<feed>&c;</feed>"""


async def test_entity_expansion_payload_not_cached():
    """A body with internal entity declarations is rejected by the hardened
    XML gate and must not enter the cache — guarding against a MITM'd upstream
    sending a billion-laughs payload that would DoS the downstream feed parser."""
    inner = _CountingInner([(200, _BILLION_LAUGHS), (200, _ATOM_OK)])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_ARXIV)  # entity-expansion body — NOT cached
        r2 = await client.get(_ARXIV)  # well-formed Atom — cached
        r3 = await client.get(_ARXIV)  # served from cache

    assert inner.calls == 2, "entity-expansion body must not be cached"
    assert transport.hits == 1
    assert r1.content == _BILLION_LAUGHS  # caller still gets the body
    assert r2.content == r3.content == _ATOM_OK


# ---------------------------------------------------------------------------
# 15. SEC-3 — well-formed Atom with no entities still admitted (regression)
# ---------------------------------------------------------------------------


async def test_normal_atom_still_admitted_after_sec3_hardening():
    """The hardened parser must not regress normal well-formed Atom feeds.
    Existing _ATOM_OK body must still be cached by the gate post-SEC-3."""
    inner = _CountingInner([(200, _ATOM_OK)])
    transport = CachingTransport(inner)

    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await client.get(_ARXIV)
        r2 = await client.get(_ARXIV)

    assert inner.calls == 1
    assert transport.hits == 1
    assert r1.content == r2.content == _ATOM_OK
