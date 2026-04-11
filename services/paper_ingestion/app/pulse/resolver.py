"""PDF resolution chain for Discovery & Pulse.

Tries arXiv → Unpaywall (→ CORE in Phase 2) to resolve a free legal PDF URL
for a given DOI / arXiv ID. Results are cached in the ``pdf_resolutions``
table so repeated requests for the same paper don't re-query external APIs.

Called lazily — NOT during Pulse scoring (would waste 100s of API calls on
papers the user never reads). Call sites:
- When a user saves or opens a Pulse card
- As a fallback in the existing ingestion pipeline when S2's pdf_url is broken
"""

from __future__ import annotations

import logging

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# Module-level flag: emit the "no Unpaywall email" info log only once per process.
_unpaywall_skip_logged: bool = False

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_pdf_url(
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    unpaywall_email: str | None = None,
) -> str | None:
    """Resolve a free legal PDF URL for a paper.

    Cache-first: returns cached result if (doi, arxiv_id) pair is already
    in pdf_resolutions. Otherwise tries each resolver in order and caches
    the first success (or caches a failure marker after all resolvers fail).

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.
    http_client:
        Shared httpx.AsyncClient for outbound HTTP calls.
    doi:
        Canonical DOI, e.g. ``"10.1234/abc"``. None allowed if arxiv_id given.
    arxiv_id:
        arXiv identifier, e.g. ``"2301.12345"``. None allowed if doi given.
    unpaywall_email:
        Required by Unpaywall ToS. If None, Unpaywall resolver is skipped.

    Returns
    -------
    str | None
        PDF URL if any resolver succeeded, None if all failed.
        None is also cached (so repeated calls don't re-try).

    Raises
    ------
    ValueError
        If both doi and arxiv_id are None.
    """
    if doi is None and arxiv_id is None:
        raise ValueError("At least one of doi or arxiv_id must be provided.")

    # 1. Cache look-up — short-circuit if we've seen this (doi, arxiv_id) before.
    try:
        cache_hit, cached_url = await _check_cache(db_pool, doi, arxiv_id)
        if cache_hit:
            return cached_url
    except Exception:
        logger.error(
            "pdf_resolutions cache read failed for doi=%s arxiv_id=%s; proceeding to resolve",
            doi,
            arxiv_id,
            exc_info=True,
        )

    # 2. Walk the resolver chain.
    # Phase 2: append _try_core here — no other change needed.
    resolver_chain = [_try_arxiv, _try_unpaywall]

    resolved_url: str | None = None
    resolver_name: str = "failed"

    for resolver_fn in resolver_chain:
        # Skip Unpaywall when email is absent (Unpaywall ToS requirement).
        if resolver_fn is _try_unpaywall and not unpaywall_email:
            _emit_unpaywall_skip_log()
            continue

        try:
            url = await resolver_fn(doi, arxiv_id, http_client, unpaywall_email)  # type: ignore[call-arg]
            if url:
                resolved_url = url
                resolver_name = resolver_fn.__name__.lstrip("_try_")
                # Map __name__ back to canonical names expected by tests/spec.
                resolver_name = _fn_to_resolver_name(resolver_fn)
                break
        except Exception:
            logger.warning(
                "Resolver %s raised an exception for doi=%s arxiv_id=%s; trying next",
                resolver_fn.__name__,
                doi,
                arxiv_id,
                exc_info=True,
            )

    # 3. Cache result (success or failure).
    try:
        await _cache_result(db_pool, doi, arxiv_id, resolved_url, resolver_name)
    except Exception:
        logger.error(
            "pdf_resolutions cache write failed for doi=%s arxiv_id=%s; returning result anyway",
            doi,
            arxiv_id,
            exc_info=True,
        )

    return resolved_url


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _fn_to_resolver_name(fn) -> str:  # type: ignore[type-arg]
    """Map private resolver function to its canonical resolver_name string."""
    _map: dict[str, str] = {
        "_try_arxiv": "arxiv",
        "_try_unpaywall": "unpaywall",
        "_try_core": "core",
    }
    name: str = str(fn.__name__)
    return _map.get(name, name)


def _emit_unpaywall_skip_log() -> None:
    """Emit the Unpaywall-skip info log once per process (module-level flag)."""
    global _unpaywall_skip_logged  # noqa: PLW0603
    if not _unpaywall_skip_logged:
        logger.info(
            "UNPAYWALL_EMAIL not configured; Unpaywall resolver skipped. "
            "Set unpaywall_email to enable open-access discovery via Unpaywall."
        )
        _unpaywall_skip_logged = True


async def _check_cache(
    db_pool: asyncpg.Pool,
    doi: str | None,
    arxiv_id: str | None,
) -> tuple[bool, str | None]:
    """Return (cache_hit, cached_url).

    cached_url may be None even on a hit (cached failure marker).

    The UNIQUE constraint on (doi, arxiv_id) uses NULL=NULL distinctness
    intentionally — each (doi, arxiv_id) pair is a separate cache entry.
    We match the exact pair here using IS NOT DISTINCT FROM to handle NULLs.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT resolved_url
              FROM pdf_resolutions
             WHERE doi IS NOT DISTINCT FROM $1
               AND arxiv_id IS NOT DISTINCT FROM $2
            """,
            doi,
            arxiv_id,
        )
    if row is None:
        return False, None
    return True, row["resolved_url"]


async def _cache_result(
    db_pool: asyncpg.Pool,
    doi: str | None,
    arxiv_id: str | None,
    url: str | None,
    resolver_name: str,
) -> None:
    """UPSERT into pdf_resolutions.

    ON CONFLICT (doi, arxiv_id) DO UPDATE so re-resolution (e.g. after a
    source outage) always reflects the latest result.
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pdf_resolutions (doi, arxiv_id, resolved_url, resolver_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (doi, arxiv_id) DO UPDATE
               SET resolved_url  = EXCLUDED.resolved_url,
                   resolver_name = EXCLUDED.resolver_name,
                   resolved_at   = NOW()
            """,
            doi,
            arxiv_id,
            url,
            resolver_name,
        )


async def _try_arxiv(
    doi: str | None,
    arxiv_id: str | None,
    http_client: httpx.AsyncClient,
    _email: str | None = None,
) -> str | None:
    """Attempt to resolve via arXiv direct PDF link.

    If arxiv_id is present, constructs
    ``https://arxiv.org/pdf/{arxiv_id}.pdf`` and issues a HEAD request.
    Returns the URL on HTTP 200, None on any other status or if no arxiv_id.
    """
    if not arxiv_id:
        return None

    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    try:
        response = await http_client.head(url, follow_redirects=True, timeout=10.0)
        if response.status_code == 200:
            return url
    except httpx.HTTPError:
        raise  # caller catches and logs

    return None


async def _try_unpaywall(
    doi: str | None,
    arxiv_id: str | None,
    http_client: httpx.AsyncClient,
    email: str | None = None,
) -> str | None:
    """Attempt to resolve via Unpaywall API.

    Calls ``GET https://api.unpaywall.org/v2/{doi}?email={email}`` and
    returns ``best_oa_location.url_for_pdf`` if present, else None.

    Returns None immediately if doi is absent (Unpaywall is DOI-based).
    """
    if not doi or not email:
        return None

    url = f"https://api.unpaywall.org/v2/{doi}"
    try:
        response = await http_client.get(
            url,
            params={"email": email},
            timeout=15.0,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        best_oa = data.get("best_oa_location") or {}
        return best_oa.get("url_for_pdf") or None
    except httpx.HTTPError:
        raise  # caller catches and logs
