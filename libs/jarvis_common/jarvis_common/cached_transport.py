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

# Structural guard: these hosts serve metadata, but export.arxiv.org also
# serves PDFs. Never buffer a binary/large body into the in-memory cache —
# keeps PDF download's stream-to-disk path intact even if a pdf_url ever
# resolves to a cache-allowlisted host.
_UNCACHEABLE_CONTENT = ("pdf", "octet-stream", "zip", "image/")


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

        key = str(request.url)
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
            raw_headers = list(response.headers.raw)
            self._store[key] = (now, response.status_code, raw_headers, body)
            self._store.move_to_end(key)
            if len(self._store) > self._max:
                self._store.popitem(last=False)
            response = httpx.Response(200, headers=raw_headers, content=body, request=request)
        self.misses += 1
        logger.debug("source-cache MISS %s h=%d m=%d", key, self.hits, self.misses)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
