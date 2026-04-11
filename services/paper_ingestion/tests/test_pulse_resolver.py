"""Tests for app.pulse.resolver — PDF resolution chain.

TDD: full coverage of cache logic, resolver chain ordering, graceful
degradation, and DB error resilience.

Uses respx for HTTP mocking and _make_pool_and_conn from conftest for
asyncpg pool mocking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

# conftest.py is auto-loaded by pytest; path setup + stubs are already in place.
from app.pulse.resolver import resolve_pdf_url

from tests.conftest import FakeRecord, _make_pool_and_conn, make_pdf_resolution_row

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOI = "10.1234/example"
_ARXIV = "2301.12345"
_PDF_URL = f"https://arxiv.org/pdf/{_ARXIV}.pdf"
_UNPAYWALL_PDF_URL = "https://oa.example.com/paper.pdf"
_EMAIL = "test@example.com"


def _pool_returning(row: FakeRecord | None):
    """Return a (pool, conn) where fetchrow yields ``row`` and execute is a no-op."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock(return_value=None)
    return pool, conn


def _pool_cache_miss():
    """Pool whose cache read returns None (cache miss) and writes succeed."""
    return _pool_returning(None)


def _pool_cache_hit(row: FakeRecord):
    """Pool whose cache read returns the given row."""
    return _pool_returning(row)


# ---------------------------------------------------------------------------
# Happy path: arXiv resolver
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_arxiv_happy_path_returns_pdf_url():
    """arxiv_id given, HEAD returns 200 → PDF URL returned and cached with 'arxiv'."""
    respx.head(_PDF_URL).mock(return_value=httpx.Response(200))

    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(pool, client, doi=_DOI, arxiv_id=_ARXIV)

    assert result == _PDF_URL

    # Cache write must have been called with resolver_name='arxiv'
    conn.execute.assert_awaited_once()
    call_args = conn.execute.await_args
    sql, doi_arg, arxiv_arg, url_arg, resolver_arg = call_args.args
    assert url_arg == _PDF_URL
    assert resolver_arg == "arxiv"


# ---------------------------------------------------------------------------
# Unpaywall fallback
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_unpaywall_fallback_returns_pdf_url():
    """arxiv_id absent, doi given → arXiv skipped, Unpaywall returns URL."""
    unpaywall_url = f"https://api.unpaywall.org/v2/{_DOI}"
    respx.get(unpaywall_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "doi": _DOI,
                "best_oa_location": {"url_for_pdf": _UNPAYWALL_PDF_URL},
            },
        )
    )

    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(
            pool, client, doi=_DOI, arxiv_id=None, unpaywall_email=_EMAIL
        )

    assert result == _UNPAYWALL_PDF_URL

    # Cache write with resolver_name='unpaywall'
    conn.execute.assert_awaited_once()
    call_args = conn.execute.await_args
    _, doi_arg, arxiv_arg, url_arg, resolver_arg = call_args.args
    assert url_arg == _UNPAYWALL_PDF_URL
    assert resolver_arg == "unpaywall"


# ---------------------------------------------------------------------------
# Cache hit — URL set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_with_url_returns_without_http():
    """Cache hit with URL → returned immediately, no HTTP call made."""
    cached_row = make_pdf_resolution_row(doi=_DOI, arxiv_id=_ARXIV, resolved_url=_PDF_URL)
    pool, conn = _pool_cache_hit(cached_row)

    # Use a transport that errors on any HTTP call — proves no network access occurs.
    def _no_http(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected HTTP call: {req.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_no_http)) as client:
        result = await resolve_pdf_url(pool, client, doi=_DOI, arxiv_id=_ARXIV)

    assert result == _PDF_URL
    # execute (cache write) must NOT have been called
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cache hit — failure marker (resolved_url = None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_failure_returns_none_without_http():
    """Cache hit with NULL resolved_url → returns None immediately, no HTTP."""
    cached_row = make_pdf_resolution_row(
        doi=_DOI, arxiv_id=_ARXIV, resolved_url=None, resolver_name="failed"
    )
    pool, conn = _pool_cache_hit(cached_row)

    def _no_http(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected HTTP call: {req.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_no_http)) as client:
        result = await resolve_pdf_url(pool, client, doi=_DOI, arxiv_id=_ARXIV)

    assert result is None
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Missing unpaywall_email
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_missing_unpaywall_email_skips_unpaywall():
    """No unpaywall_email → Unpaywall skipped; if arXiv also fails → None + 'failed' cached."""
    # arXiv HEAD returns 404
    respx.head(_PDF_URL).mock(return_value=httpx.Response(404))

    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(
            pool, client, doi=_DOI, arxiv_id=_ARXIV, unpaywall_email=None
        )

    assert result is None

    # Cache write with resolver_name='failed'
    conn.execute.assert_awaited_once()
    call_args = conn.execute.await_args
    _, doi_arg, arxiv_arg, url_arg, resolver_arg = call_args.args
    assert url_arg is None
    assert resolver_arg == "failed"


@respx.mock
@pytest.mark.asyncio
async def test_missing_unpaywall_email_only_arxiv_attempted():
    """When email absent, only arXiv is tried; arXiv succeeds → URL returned."""
    respx.head(_PDF_URL).mock(return_value=httpx.Response(200))

    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(
            pool, client, doi=_DOI, arxiv_id=_ARXIV, unpaywall_email=None
        )

    assert result == _PDF_URL


# ---------------------------------------------------------------------------
# Unpaywall 404 → graceful
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_unpaywall_404_graceful():
    """Unpaywall returns 404 → _try_unpaywall returns None gracefully; result cached as 'failed'."""
    # No arXiv id, so arXiv resolver returns None without HTTP.
    unpaywall_url = f"https://api.unpaywall.org/v2/{_DOI}"
    respx.get(unpaywall_url).mock(return_value=httpx.Response(404))

    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(
            pool, client, doi=_DOI, arxiv_id=None, unpaywall_email=_EMAIL
        )

    assert result is None
    conn.execute.assert_awaited_once()
    call_args = conn.execute.await_args
    _, _, _, url_arg, resolver_arg = call_args.args
    assert url_arg is None
    assert resolver_arg == "failed"


# ---------------------------------------------------------------------------
# ValueError on missing identifiers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_identifiers_none_raises_value_error():
    """Both doi and arxiv_id None → ValueError raised before any DB access."""
    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="doi or arxiv_id"):
            await resolve_pdf_url(pool, client, doi=None, arxiv_id=None)

    # DB must not have been touched
    conn.fetchrow.assert_not_awaited()
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# DB error during cache read → resolver chain still runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_read_error_resolver_chain_still_executes():
    """DB error on cache read → resolver chain still runs, result returned."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow = AsyncMock(side_effect=Exception("DB unavailable"))
    conn.execute = AsyncMock(return_value=None)

    with respx.mock:
        respx.head(_PDF_URL).mock(return_value=httpx.Response(200))

        async with httpx.AsyncClient() as client:
            result = await resolve_pdf_url(pool, client, doi=_DOI, arxiv_id=_ARXIV)

    # Despite DB error, arXiv resolved successfully
    assert result == _PDF_URL


# ---------------------------------------------------------------------------
# DB error during cache write → result returned anyway
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_db_write_error_result_returned_anyway():
    """DB error on cache write → resolved URL still returned (no exception raised)."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow = AsyncMock(return_value=None)  # cache miss
    conn.execute = AsyncMock(side_effect=Exception("DB write failed"))

    respx.head(_PDF_URL).mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(pool, client, doi=_DOI, arxiv_id=_ARXIV)

    assert result == _PDF_URL  # DB write error must not propagate


# ---------------------------------------------------------------------------
# Resolver exception (e.g., network timeout) → caught, next resolver tried
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_resolver_exception_falls_through_to_next():
    """arXiv HEAD raises ConnectTimeout → caught, Unpaywall tried, succeeds."""
    respx.head(_PDF_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))

    unpaywall_url = f"https://api.unpaywall.org/v2/{_DOI}"
    respx.get(unpaywall_url).mock(
        return_value=httpx.Response(
            200,
            json={"best_oa_location": {"url_for_pdf": _UNPAYWALL_PDF_URL}},
        )
    )

    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(
            pool, client, doi=_DOI, arxiv_id=_ARXIV, unpaywall_email=_EMAIL
        )

    assert result == _UNPAYWALL_PDF_URL

    call_args = conn.execute.await_args
    _, _, _, url_arg, resolver_arg = call_args.args
    assert resolver_arg == "unpaywall"


@respx.mock
@pytest.mark.asyncio
async def test_all_resolvers_raise_caches_failed():
    """Both resolvers raise → 'failed' cached, None returned."""
    respx.head(_PDF_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    unpaywall_url = f"https://api.unpaywall.org/v2/{_DOI}"
    respx.get(unpaywall_url).mock(side_effect=httpx.ConnectTimeout("timeout"))

    pool, conn = _pool_cache_miss()

    async with httpx.AsyncClient() as client:
        result = await resolve_pdf_url(
            pool, client, doi=_DOI, arxiv_id=_ARXIV, unpaywall_email=_EMAIL
        )

    assert result is None
    call_args = conn.execute.await_args
    _, _, _, url_arg, resolver_arg = call_args.args
    assert url_arg is None
    assert resolver_arg == "failed"
