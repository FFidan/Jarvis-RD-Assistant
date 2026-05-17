"""Bounded in-memory cache for external metadata-source GETs.

Reopens docs/perf/2026-05-12-library-wishlist-decisions.md "httpx-cache"
under its stated criteria: short TTL, transparent error behavior (only 200
GETs to an explicit host allowlist are cached — 429/5xx and every non-source
host pass straight through), hit/miss counters, env opt-out. No third-party
dependency (the decision record's httpx-compat concern).
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict

import httpx
import lxml.etree as _etree

logger = logging.getLogger(__name__)

_SOURCE_HOSTS = frozenset(
    {
        "api.semanticscholar.org",
        "export.arxiv.org",
        "api.openalex.org",
        "api.crossref.org",
        "eutils.ncbi.nlm.nih.gov",
    }
)

# Hosts whose 200 bodies are XML/Atom. A transient bad/empty body here parses
# fine as "text" but breaks the downstream feed parser, so we additionally
# require well-formedness before caching (the rest are JSON APIs — empty-body
# check below is enough). Crossref/OpenAlex/S2 are JSON REST and stay off this
# list so a JSON body is never run through the XML parser.
_XML_SOURCE_HOSTS = frozenset({"export.arxiv.org", "eutils.ncbi.nlm.nih.gov"})

# Structural guard: these hosts serve metadata, but export.arxiv.org also
# serves PDFs. Never buffer a binary/large body into the in-memory cache —
# keeps PDF download's stream-to-disk path intact even if a pdf_url ever
# resolves to a cache-allowlisted host.
_UNCACHEABLE_CONTENT = ("pdf", "octet-stream", "zip", "image/")

# Credential-bearing query params (NCBI E-utilities / OpenAlex pass the key in
# the query string). Excluded from the cache key so the secret never lands in
# the in-memory store or the debug log — and because two requests differing
# only by credential address the same upstream resource anyway.
_SENSITIVE_QUERY_PARAMS = frozenset(
    {"api_key", "apikey", "api-key", "key", "token", "access_token"}
)

# Hop-by-hop headers (RFC 7230 §6.1) plus content-coding/length: meaningless or
# wrong once the body is buffered and decoded by httpx, so they must not be
# stored and replayed on a cache hit.
_STRIP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"content-encoding",
        b"content-length",
    }
)


def _cache_key(url: httpx.URL) -> str:
    """URL string with credential query params removed."""
    for name in [k for k in url.params.keys() if k.lower() in _SENSITIVE_QUERY_PARAMS]:
        url = url.copy_remove_param(name)
    return str(url)


def _safe_headers(raw: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Drop hop-by-hop / stale framing headers from a to-be-cached response."""
    return [(name, value) for name, value in raw if name.lower() not in _STRIP_HEADERS]


# Module-level parser so the config is constructed once, not per-call.
# Mirrors the _SAFE_PARSER in paper_ingestion.sources._xml_safe — both must
# stay in sync if options change. Jarvis-common must not import from services
# (tach boundary), so the config is duplicated here intentionally.
_SAFE_XML_PARSER = _etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
)


def _is_well_formed_xml(body: bytes) -> bool:
    """True if ``body`` is a well-formed XML document with no entity references.

    Uses lxml with entity resolution, network access, DTD loading, and
    huge_tree all disabled. A body that still contains entity references after
    parsing (e.g. billion-laughs payloads a MITM'd upstream could send) is
    treated as unsafe and rejected — the entity references survive in the tree
    only because expansion was refused, not because the body is safe to cache
    and replay to the downstream feed parser.
    """
    try:
        root = _etree.fromstring(body, parser=_SAFE_XML_PARSER)
    except _etree.XMLSyntaxError:
        return False
    # Any surviving _Entity nodes mean the body declared entities that lxml
    # refused to expand — reject so adversarial bodies never enter the cache.
    return not any(True for _ in root.iter(_etree.Entity))


def _is_cacheable_body(host: str, body: bytes) -> bool:
    """Only genuine usable bodies enter the cache: never empty, and for XML
    source hosts the body must parse as well-formed XML (a transient bad/empty
    arXiv response must not be served for the whole TTL)."""
    if not body:
        return False
    if host in _XML_SOURCE_HOSTS:
        return _is_well_formed_xml(body)
    return True


def _env_enabled() -> bool:
    return os.getenv("SOURCE_HTTP_CACHE_ENABLED", "true").strip().lower() != "false"


def _env_ttl() -> float:
    try:
        return float(os.getenv("SOURCE_HTTP_CACHE_TTL_SECONDS", "900"))
    except ValueError:
        return 900.0


class CachingTransport(httpx.AsyncBaseTransport):
    """Caches GET+200 for source hosts only. Everything else is passthrough."""

    def __init__(self, inner: httpx.AsyncBaseTransport, *, max_entries: int = 512) -> None:
        self._inner = inner
        self._enabled = _env_enabled()
        self._ttl = _env_ttl()
        self._max = max_entries
        self._store: OrderedDict[str, tuple[float, int, list[tuple[bytes, bytes]], bytes]] = (
            OrderedDict()
        )
        self.hits = 0
        self.misses = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._enabled or request.method != "GET" or request.url.host not in _SOURCE_HOSTS:
            return await self._inner.handle_async_request(request)

        key = _cache_key(request.url)
        now = time.monotonic()
        hit = self._store.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            self.hits += 1
            self._store.move_to_end(key)
            logger.debug("source-cache HIT %s h=%d m=%d", key, self.hits, self.misses)
            return httpx.Response(hit[1], headers=hit[2], content=hit[3], request=request)

        response = await self._inner.handle_async_request(request)
        ctype = response.headers.get("content-type", "").lower()
        is_binary = any(tok in ctype for tok in _UNCACHEABLE_CONTENT)
        if response.status_code == 200 and not is_binary:
            body = await response.aread()
            safe_headers = _safe_headers(response.headers.raw)
            if _is_cacheable_body(request.url.host, body):
                self._store[key] = (now, response.status_code, safe_headers, body)
                self._store.move_to_end(key)
                if len(self._store) > self._max:
                    self._store.popitem(last=False)
            else:
                logger.debug("source-cache SKIP (empty/malformed body) %s", key)
            # Body was consumed by aread(); rebuild a fresh response either way so
            # the caller still gets it (uncached on skip → can transiently retry).
            response = httpx.Response(200, headers=safe_headers, content=body, request=request)
        self.misses += 1
        logger.debug("source-cache MISS %s h=%d m=%d", key, self.hits, self.misses)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
