"""PubMed paper source implementation.

Uses NCBI E-utilities (https://www.ncbi.nlm.nih.gov/books/NBK25497/):
- ``esearch.fcgi`` — converts a search query into a list of PMIDs.
- ``efetch.fcgi`` — retrieves full PubMed XML records for a list of PMIDs.

The NCBI API is usable without a key at 3 requests/second. Providing a
``PUBMED_API_KEY`` environment variable (or ``api_key`` in source config)
upgrades the rate limit to 10 requests/second. The source supports unauthenticated
requests and is enabled by default.

XML parsing uses ``sources._xml_safe`` (shared XXE-safe lxml wrapper) for robust,
namespace-aware handling.
PubMed structured abstracts (multiple ``<AbstractText>`` elements with
``Label`` attributes) are concatenated with the label prefix to preserve
structure.

Published date handling
-----------------------
PubMed records may have:
1. ``<ArticleDate DateType="Electronic">`` — precise electronic pub date.
2. ``<PubDate>`` with ``<Year>``, ``<Month>``, ``<Day>`` (month/day may be absent).

This plugin prefers ArticleDate when present; falls back to PubDate, using
the first of the month when day is absent, and January 1 when month is absent.
"""

import logging
import time as _time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

import httpx
from jarvis_common.maintenance import OutboundEgressBlockedError, ensure_outbound_egress_allowed
from jarvis_common.source_rate_limiter import SourceRateLimiter
from lxml import (
    etree,  # type: ignore[reportAttributeAccessIssue]  # lxml stubs lack etree export typing
)

from paper_ingestion.models import PaperCreate, PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources._xml_safe import safe_fromstring
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.registry import register_source


def _parse_xml(content: bytes) -> etree._Element:
    """Parse *content* with the shared XXE-safe XMLParser."""
    return safe_fromstring(content)


logger = logging.getLogger(__name__)

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH_URL = f"{NCBI_BASE}/esearch.fcgi"
EFETCH_URL = f"{NCBI_BASE}/efetch.fcgi"

_MONTH_MAP = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _parse_month(month_str: str | None) -> int | None:
    """Parse a month string (numeric or abbreviated name) to an int 1-12."""
    if not month_str:
        return None
    month_str = month_str.strip()
    if month_str.isdigit():
        return int(month_str)
    return _MONTH_MAP.get(month_str[:3].capitalize())


def _parse_pub_date(article_el: etree._Element) -> date | None:  # noqa: SLF001
    """Extract the best available publication date from a PubMed Article element.

    Strategy:
    1. ``ArticleDate[@DateType='Electronic']`` — most precise.
    2. ``PubDate`` under ``JournalIssue/PubDate``.
    Falls back to year-only (January 1) when day/month are absent.
    """
    # 1. Try ArticleDate (Electronic)
    for art_date in article_el.findall(".//ArticleDate"):
        if art_date.get("DateType") == "Electronic":
            year_el = art_date.find("Year")
            month_el = art_date.find("Month")
            day_el = art_date.find("Day")
            if year_el is not None and year_el.text:
                try:
                    year = int(year_el.text)
                    month = _parse_month(month_el.text if month_el is not None else None) or 1
                    day = int(day_el.text) if day_el is not None and day_el.text else 1
                    return date(year, month, day)
                except (ValueError, OverflowError) as exc:
                    logger.debug("PubMed: malformed ArticleDate value — skipping: %s", exc)

    # 2. Fall back to PubDate (may be under JournalIssue or directly under Article)
    pub_date_el = article_el.find(".//JournalIssue/PubDate")
    if pub_date_el is None:
        pub_date_el = article_el.find(".//PubDate")
    if pub_date_el is not None:
        year_el = pub_date_el.find("Year")
        month_el = pub_date_el.find("Month")
        day_el = pub_date_el.find("Day")
        if year_el is not None and year_el.text:
            try:
                year = int(year_el.text)
                month = _parse_month(month_el.text if month_el is not None else None) or 1
                day = int(day_el.text) if day_el is not None and day_el.text else 1
                return date(year, month, day)
            except (ValueError, OverflowError) as exc:
                logger.debug("PubMed: malformed PubDate value — skipping: %s", exc)

    return None


def _parse_abstract(article_el: etree._Element) -> str | None:  # noqa: SLF001
    """Concatenate all AbstractText elements into a single abstract string.

    PubMed structured abstracts carry multiple ``<AbstractText Label="...">``
    sections (BACKGROUND, METHODS, RESULTS, CONCLUSIONS, etc.).  Each section
    is prefixed with its label (if present) and joined by a newline.
    """
    abstract_el = article_el.find(".//Abstract")
    if abstract_el is None:
        return None

    parts = []
    for text_el in abstract_el.findall("AbstractText"):
        label = text_el.get("Label")
        text = (text_el.text or "").strip()
        if text:
            if label:
                parts.append(f"{label}: {text}")
            else:
                parts.append(text)

    return "\n".join(parts) if parts else None


def _parse_authors(article_el: etree._Element) -> list[str]:  # noqa: SLF001
    """Extract author full names from a PubMed Article element."""
    authors = []
    for author_el in article_el.findall(".//AuthorList/Author"):
        last = (author_el.findtext("LastName") or "").strip()
        fore = (author_el.findtext("ForeName") or "").strip()
        if last and fore:
            authors.append(f"{fore} {last}")
        elif last:
            authors.append(last)
    return authors


def _parse_doi(medline_el: etree._Element) -> str | None:  # noqa: SLF001
    """Extract DOI from ArticleIdList in a MedlineCitation element."""
    for aid in medline_el.findall(".//ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            return (aid.text or "").strip() or None
    return None


def _parse_article(medline_el: etree._Element) -> PaperCreate:  # noqa: SLF001
    """Parse a single MedlineCitation element into a PaperCreate model.

    Parameters
    ----------
    medline_el : etree._Element
        A ``<MedlineCitation>`` element from an ``<efetch>`` response.

    Returns
    -------
    PaperCreate
        Paper with metadata from PubMed API (never LLM-generated).
    """
    pmid = (medline_el.findtext("PMID") or "").strip()
    article_el = medline_el.find("Article")
    if article_el is None:
        raise ValueError(f"No <Article> element for PMID {pmid}")

    title = (article_el.findtext("ArticleTitle") or "").strip()
    abstract = _parse_abstract(article_el)
    authors = _parse_authors(article_el)
    published_date = _parse_pub_date(article_el)
    doi = _parse_doi(medline_el)

    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    metadata: dict[str, Any] = {"pmid": pmid}
    if doi:
        metadata["doi"] = doi

    return PaperCreate(
        external_id=f"pubmed:{pmid}",
        source_type=SourceType.PUBMED,
        title=title,
        authors=authors,
        abstract=abstract,
        published_date=published_date,
        url=url,
        pdf_url=None,
        metadata=metadata,
    )


@register_source
class PubMedSource(PaperSource):
    """PubMed NCBI E-utilities paper source.

    Attributes
    ----------
    source_type : str
        Always ``"pubmed"``.

    Notes
    -----
    Network methods propagate ``OutboundEgressBlockedError`` while restored
    credentials await review; quarantine is not reported as an empty provider
    result.
    """

    source_type = "pubmed"

    def __init__(
        self,
        config: PaperSourceConfig,
        http_client: httpx.AsyncClient,
        db_pool: "asyncpg.Pool | None" = None,
    ) -> None:
        super().__init__(config, http_client, db_pool)
        from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

        _cfg = get_paper_ingestion_settings()
        self._api_key: str | None = self._resolve_api_key(_cfg.pubmed_api_key)
        # rate: 10 req/s with API key, ~3 req/s otherwise
        self._rate_interval = 0.1 if self._api_key else 0.34
        self._rate_limiter = SourceRateLimiter(rate_per_second=1.0 / self._rate_interval)
        # NCBI E-utilities best-practice: identify the calling application.
        self._ncbi_tool: str = _cfg.ncbi_tool
        self._ncbi_email: str = _cfg.ncbi_email
        self._ncbi_user_agent: str = (
            f"JARVIS-RD/1.0 (tool={self._ncbi_tool}; contact={self._ncbi_email or 'unset'})"
        )

    async def _rate_limit(self) -> None:
        """Enforce NCBI rate limit: 10 req/s with API key, ~3 req/s otherwise."""
        await self._rate_limiter.acquire()

    def _base_params(self) -> dict:
        """Build common NCBI E-utilities parameters.

        Includes ``tool`` and ``email`` per NCBI best-practice so NCBI can
        attribute and contact the calling application rather than blanket-block.
        """
        params: dict = {"db": "pubmed", "retmode": "xml", "tool": self._ncbi_tool}
        if self._ncbi_email:
            params["email"] = self._ncbi_email
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def _ncbi_headers(self) -> dict[str, str]:
        """Return HTTP headers for NCBI E-utilities requests."""
        return {"User-Agent": self._ncbi_user_agent}

    async def _esearch(self, term: str, retmax: int, extra: dict | None = None) -> list[str]:
        """Run an esearch query and return a list of PMIDs.

        Parameters
        ----------
        term : str
            PubMed search term (supports full PubMed query syntax).
        retmax : int
            Maximum number of PMIDs to return.
        extra : dict | None
            Additional query parameters (e.g. ``mindate``, ``maxdate``).

        Returns
        -------
        list[str]
            PMID strings, or ``[]`` on HTTP errors / XML parse errors.

        Raises
        ------
        OutboundEgressBlockedError
            If restored credentials await review before the request.
        """
        ensure_outbound_egress_allowed("PubMed search")
        await self._rate_limit()
        params = self._base_params()
        params.update({"term": term, "retmax": retmax})
        if extra:
            params.update(extra)
        try:
            ensure_outbound_egress_allowed("PubMed search")
            response = await self.http_client.get(
                ESEARCH_URL, params=params, headers=self._ncbi_headers(), timeout=30.0
            )
            if response.status_code in (429, 500, 502, 503, 504):
                logger.warning("PubMed esearch returned %d", response.status_code)
                self._record_transient_poll_diagnostic(response)
                return []
            response.raise_for_status()
            self._clear_poll_diagnostic()
            root = _parse_xml(response.content)
        except (httpx.HTTPError, etree.XMLSyntaxError) as exc:
            logger.warning("PubMed esearch failed: %s", exc)
            response = getattr(exc, "response", None)
            if response is not None:
                self._record_transient_poll_diagnostic(response)
            else:
                self._set_poll_diagnostic(
                    status="error",
                    message=str(exc),
                    status_code=None,
                    retry_after_s=None,
                    settings_hint=None,
                )
            return []

        return [el.text for el in root.findall(".//IdList/Id") if el.text]

    async def _efetch(self, pmids: list[str]) -> list[PaperCreate]:
        """Fetch full records for a list of PMIDs and parse them.

        Parameters
        ----------
        pmids : list[str]
            PubMed IDs to fetch.

        Returns
        -------
        list[PaperCreate]
            Parsed papers.  Individual parse failures are skipped with a
            warning; HTTP or XML errors return ``[]``.

        Raises
        ------
        OutboundEgressBlockedError
            If restored credentials await review before a non-empty fetch.
        """
        if not pmids:
            return []
        ensure_outbound_egress_allowed("PubMed record fetch")
        await self._rate_limit()
        params = self._base_params()
        params["id"] = ",".join(pmids)
        try:
            ensure_outbound_egress_allowed("PubMed record fetch")
            response = await self.http_client.get(
                EFETCH_URL, params=params, headers=self._ncbi_headers(), timeout=60.0
            )
            if response.status_code in (429, 500, 502, 503, 504):
                logger.warning("PubMed efetch returned %d", response.status_code)
                self._record_transient_poll_diagnostic(response)
                return []
            response.raise_for_status()
            self._clear_poll_diagnostic()
            root = _parse_xml(response.content)
        except (httpx.HTTPError, etree.XMLSyntaxError) as exc:
            logger.warning("PubMed efetch failed: %s", exc)
            response = getattr(exc, "response", None)
            if response is not None:
                self._record_transient_poll_diagnostic(response)
            else:
                self._set_poll_diagnostic(
                    status="error",
                    message=str(exc),
                    status_code=None,
                    retry_after_s=None,
                    settings_hint=None,
                )
            return []

        papers = []
        for medline_el in root.findall(".//MedlineCitation"):
            try:
                papers.append(_parse_article(medline_el))
            except Exception:
                pmid = medline_el.findtext("PMID") or "unknown"
                logger.exception("PubMed: failed to parse PMID %s", pmid)
        return papers

    async def search(
        self,
        query: str,
        max_results: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        sort_by: str = "relevance",
        author: str | None = None,
    ) -> list[PaperCreate]:
        """Search PubMed for papers matching the query.

        Uses esearch → efetch pipeline: first obtains PMIDs, then retrieves
        full records.

        Parameters
        ----------
        query : str
            PubMed query string (supports MeSH terms, field tags, etc.).
        max_results : int
            Maximum number of results to return.
        year_from : int | None
            Filter to papers published from this year (inclusive).
        year_to : int | None
            Filter to papers published up to this year (inclusive).
        sort_by : str
            Sort order: ``"relevance"`` (default) or ``"date"``.
        author : str | None
            Filter results by author name.

        Returns
        -------
        list[PaperCreate]
            Papers parsed from PubMed XML.  Returns ``[]`` on HTTP errors.
        """
        term = query
        if author:
            # Wrap in double-quotes so NCBI treats the value as a phrase literal.
            # Strip embedded double-quotes first to prevent escaping out of the
            # quoted phrase and injecting additional query syntax.
            safe_author = author.replace('"', "")
            term = f'{term} AND "{safe_author}"[Author]'

        extra: dict = {}
        if year_from or year_to:
            extra["datetype"] = "pdat"
            extra["mindate"] = str(year_from or 1800)
            extra["maxdate"] = str(year_to or 2100)
        if sort_by == "date":
            extra["sort"] = "pub_date"

        pmids = await self._esearch(term, retmax=max_results, extra=extra or None)
        if not pmids:
            return []
        return await self._efetch(pmids)

    async def fetch_by_id(self, external_id: str) -> PaperCreate | None:
        """Fetch a single PubMed paper by PMID.

        Parameters
        ----------
        external_id : str
            PMID, with or without ``"pubmed:"`` prefix.

        Returns
        -------
        PaperCreate | None
            The paper if found, ``None`` otherwise.
        """
        pmid = external_id.removeprefix("pubmed:")
        papers = await self._efetch([pmid])
        return papers[0] if papers else None

    async def fetch_new_since(
        self,
        since: datetime,
        topics: list[TopicRef],
        limit: int = 100,
        user_id: int | None = None,
    ) -> list[PaperCreate]:
        """Fetch PubMed papers published after *since* relevant to the given topics.

        Uses the NCBI ``mindate``/``maxdate`` parameters combined with per-topic
        query terms.  One esearch+efetch pipeline is executed per topic; results
        are deduplicated by PMID.

        Parameters
        ----------
        since : datetime
            Lower bound for publication date; converted to ``YYYY/MM/DD`` for NCBI.
        topics : list[TopicRef]
            Topics to include.  An empty list triggers a single undirected date query.
        limit : int
            Maximum total papers to return.
        user_id : int or None
            Caller identity for per-user rate limiting and run history, when
            available.

        Returns
        -------
        list[PaperCreate]
            Papers published after *since*. Returns ``[]`` on HTTP errors.

        Raises
        ------
        OutboundEgressBlockedError
            If restored credentials await review before or during a scheduled
            request.
        """
        # Startup grace — lets containers finish their warm-up before first burst.
        await self.apply_startup_grace()

        # Persistent rate limiter (no-op when db_pool is None).
        p_limiter = self.make_persistent_rate_limiter(
            user_id=user_id, min_interval_seconds=self._rate_interval
        )

        since_utc = self._normalize_since_utc(since)
        mindate = since_utc.strftime("%Y/%m/%d")
        date_params = {"mindate": mindate, "datetype": "pdat"}

        if not topics:
            term_queries = [""]
        else:
            term_queries = []
            for topic in topics:
                terms = topic.query_terms if topic.query_terms else [topic.name]
                term_queries.append(" OR ".join(f'"{t}"' for t in terms))

        seen_pmids: set[str] = set()
        papers: list[PaperCreate] = []
        per_q = max(1, limit // max(len(term_queries), 1))

        started_at = _time.monotonic()
        candidate_count = 0

        try:
            for term in term_queries:
                if len(papers) >= limit:
                    break
                if p_limiter is not None:
                    await p_limiter.acquire()
                pmids = await self._esearch(term or "pubmed[sb]", retmax=per_q, extra=date_params)
                new_pmids = [p for p in pmids if p not in seen_pmids]
                if not new_pmids:
                    continue
                seen_pmids.update(new_pmids)
                fetched = await self._efetch(new_pmids)
                papers.extend(fetched)
                candidate_count += len(fetched)
                if len(papers) >= limit:
                    break
        except OutboundEgressBlockedError:
            raise
        except Exception as _exc:
            logger.warning("pubmed: fetch_new_since failed", exc_info=True)
            # PubMed has only two terminal paths: _esearch/_efetch swallow
            # transient HTTP errors internally (returning []), so a no-data run
            # falls through to the success path with candidate_count=0.  Only an
            # *unexpected* exception escaping the loop reaches this error branch.
            # The diagnostic's Retry-After (set by an earlier 429) is forwarded
            # so the persistent limiter still backs off.
            diag = self.last_poll_diagnostic or {}
            await self._record_poll_exception(
                started_at=started_at,
                user_id=user_id,
                p_limiter=p_limiter,
                log_context={"http_status": None, "exception": repr(_exc)[:300]},
                retry_after_s=diag.get("retry_after_s"),
            )
            return papers[:limit]

        await self._record_poll_success(
            started_at=started_at,
            candidate_count=candidate_count,
            query_count=len(term_queries),
            user_id=user_id,
            p_limiter=p_limiter,
        )

        return papers[:limit]
