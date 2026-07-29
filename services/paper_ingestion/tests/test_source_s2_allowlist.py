"""Tests that SemanticScholarSource filters openAccessPdf.url via ALLOWED_PDF_DOMAINS.

S2's openAccessPdf field can point to arbitrary third-party hosts.  The fix
validates pdf_url at parse time so the DB never receives unsafe URLs.
"""

from __future__ import annotations

import logging

import httpx
import respx
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from paper_ingestion.sources.semantic_scholar_source import S2_API_URL
from tests._source_fakes import make_semantic_scholar_source as _make_source


def _s2_paper(paper_id: str, open_access_pdf: dict | None) -> dict:
    return {
        "paperId": paper_id,
        "title": "Test Paper",
        "authors": [{"name": "Author A", "authorId": "1"}],
        "abstract": "Abstract text",
        "year": 2026,
        "publicationDate": "2026-01-01",
        "url": f"https://www.semanticscholar.org/paper/{paper_id}",
        "citationCount": 0,
        "externalIds": {},
        "openAccessPdf": open_access_pdf,
        "tldr": None,
    }


# pdf_url validated against ALLOWED_PDF_DOMAINS allowlist at parse time
# ---------------------------------------------------------------------------


@respx.mock
async def test_s2_source_filters_pdf_url_by_allowlist_blocked(caplog):
    """_parse_paper: openAccessPdf.url with a hostname not in ALLOWED_PDF_DOMAINS is set to None.

    An unrecognised domain must be discarded and an INFO log must be emitted so
    that operators can see which URLs were rejected.
    """
    blocked_url = "https://evil.com/paper.pdf"
    source = _make_source()

    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_s2_paper("p_evil", {"url": blocked_url})], "next": None},
        )
    )

    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.semantic_scholar_source"):
        papers = await source.search("neural ode")

    assert len(papers) == 1
    assert papers[0].pdf_url is None, "Blocked domain must yield pdf_url=None in DB"
    assert any("ALLOWED_PDF_DOMAINS" in r.message for r in caplog.records), (
        "INFO log mentioning ALLOWED_PDF_DOMAINS must be emitted for rejected URL"
    )


@respx.mock
async def test_s2_source_filters_pdf_url_by_allowlist_allowed(caplog):
    """_parse_paper: openAccessPdf.url on an allowlisted hostname is preserved."""
    allowed_host = next(iter(ALLOWED_PDF_DOMAINS))  # e.g. "arxiv.org"
    allowed_url = f"https://{allowed_host}/pdf/2601.00001"
    source = _make_source()

    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_s2_paper("p_arxiv", {"url": allowed_url})], "next": None},
        )
    )

    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.semantic_scholar_source"):
        papers = await source.search("neural ode")

    assert len(papers) == 1
    assert papers[0].pdf_url == allowed_url, "Allowlisted domain must preserve pdf_url"
    assert not any("ALLOWED_PDF_DOMAINS" in r.message for r in caplog.records), (
        "No ALLOWED_PDF_DOMAINS log should be emitted for an accepted URL"
    )


@respx.mock
async def test_s2_source_filters_pdf_url_null_open_access_pdf(caplog):
    """_parse_paper: openAccessPdf=None (no open-access URL available) → pdf_url=None."""
    source = _make_source()

    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_s2_paper("p_null", None)], "next": None},
        )
    )

    papers = await source.search("neural ode")

    assert len(papers) == 1
    assert papers[0].pdf_url is None
