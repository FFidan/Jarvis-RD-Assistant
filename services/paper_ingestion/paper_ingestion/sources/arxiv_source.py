"""arXiv paper source implementation.

Uses the arXiv Atom API (https://export.arxiv.org/api/query).
Rate limit: 3 requests/second per arXiv Terms of Use.
"""

import logging
import re
from datetime import UTC, date, datetime
from typing import Any

import httpx
from jarvis_common.source_rate_limiter import SourceRateLimiter

from paper_ingestion.models import PaperCreate, PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources._xml_safe import safe_fromstring
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.registry import register_source

logger = logging.getLogger(__name__)

# arXiv Atom XML namespaces
ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"

ARXIV_API_URL = "https://export.arxiv.org/api/query"
RATE_LIMIT_DELAY = 0.34  # seconds between requests (~3 req/sec)
_ARXIV_FIELD_PREFIX = re.compile(r"\b(ti|au|abs|co|jr|cat|rn|id|all):")


@register_source
class ArxivSource(PaperSource):
    """arXiv API paper source.

    Attributes
    ----------
    source_type : str
        Always ``"arxiv"``.
    """

    source_type = "arxiv"

    def __init__(self, config: PaperSourceConfig, http_client: httpx.AsyncClient) -> None:
        super().__init__(config, http_client)
        # rate: 1/RATE_LIMIT_DELAY req/s (arXiv 3 req/s limit)
        self._rate_limiter = SourceRateLimiter(rate_per_second=1.0 / RATE_LIMIT_DELAY)

    async def _rate_limit(self) -> None:
        """Enforce arXiv 3 req/sec rate limit."""
        await self._rate_limiter.acquire()

    async def _fetch_xml(self, params: dict) -> Any | None:
        """Rate-limited GET to the arXiv API; returns parsed XML or None on transient errors.

        Uses :meth:`PaperSource._safe_get` so that 429 / 5xx responses degrade
        gracefully (return ``None``) instead of raising to callers.  Non-transient
        error codes (e.g. 403) still raise :class:`httpx.HTTPStatusError`.
        """
        await self._rate_limit()
        response = await self._safe_get(ARXIV_API_URL, params=params, timeout=30.0)
        if response is None:
            return None
        return safe_fromstring(response.text)

    def _parse_entry(self, entry: Any) -> PaperCreate:
        """Parse a single Atom ``<entry>`` element into a PaperCreate model.

        Parameters
        ----------
        entry : Element
            An ``<entry>`` element from the arXiv Atom feed.

        Returns
        -------
        PaperCreate
            Paper with all metadata from arXiv API (never LLM-generated).
        """
        # Extract arXiv ID from the <id> URL: "http://arxiv.org/abs/2301.12345v1"
        raw_id = entry.findtext(f"{{{ATOM_NS}}}id", default="")
        arxiv_id = raw_id.split("/abs/")[-1]
        # Strip version suffix for canonical ID
        canonical_id = arxiv_id.rsplit("v", 1)[0] if "v" in arxiv_id else arxiv_id

        title = entry.findtext(f"{{{ATOM_NS}}}title", default="").strip().replace("\n", " ")

        authors = [
            author.findtext(f"{{{ATOM_NS}}}name", default="")
            for author in entry.findall(f"{{{ATOM_NS}}}author")
        ]

        abstract = entry.findtext(f"{{{ATOM_NS}}}summary", default="").strip()

        # Published date: "2023-01-15T12:00:00Z"
        published_str = entry.findtext(f"{{{ATOM_NS}}}published", default="")
        published_date: date | None = None
        if published_str:
            published_date = date.fromisoformat(published_str[:10])

        # Links: find PDF and abstract page links
        pdf_url: str | None = None
        abs_url = raw_id  # fallback
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")
            elif link.get("rel") == "alternate":
                abs_url = link.get("href", abs_url)

        # Categories
        categories = [
            cat.get("term", "") for cat in entry.findall(f"{{{ARXIV_NS}}}primary_category")
        ]
        categories.extend(cat.get("term", "") for cat in entry.findall(f"{{{ATOM_NS}}}category"))

        return PaperCreate(
            external_id=f"arxiv:{canonical_id}",
            source_type=SourceType.ARXIV,
            title=title,
            authors=authors,
            abstract=abstract,
            published_date=published_date,
            url=abs_url,
            pdf_url=pdf_url,
            metadata={"categories": list(set(categories)), "arxiv_id": canonical_id},
        )

    def _build_search_query(self, raw_query: str) -> str:
        """Build an arXiv API search query from user input.

        If the user provides structured syntax (ti:, cat:, au:, etc.), use as-is.
        Otherwise, search title + abstract fields for better relevance.
        """
        # Pass through structured queries unchanged
        if _ARXIV_FIELD_PREFIX.search(raw_query):
            return raw_query

        # Clean up user input
        safe = raw_query.replace('"', "").strip()
        if not safe:
            return f"all:{raw_query}"

        # Search title OR abstract with the full phrase — much more relevant than all:
        return f'(ti:"{safe}" OR abs:"{safe}")'

    async def search(
        self,
        query: str,
        max_results: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        sort_by: str = "relevance",
        author: str | None = None,
    ) -> list[PaperCreate]:
        """Search arXiv for papers matching the query.

        Parameters
        ----------
        query : str
            arXiv search query (supports arXiv query syntax like
            ``"cat:cs.AI AND ti:transformer"``).
        max_results : int
            Maximum results to return.
        year_from : int | None
            Filter to papers submitted from this year (inclusive).
        year_to : int | None
            Filter to papers submitted up to this year (inclusive).
        sort_by : str
            Sort order: ``"relevance"`` (default) or ``"date"``.
        author : str | None
            Filter results by author name.

        Returns
        -------
        list[PaperCreate]
            Papers parsed from arXiv API response.
        """
        search_query = self._build_search_query(query)

        if author:
            search_query = f"au:{author} AND {search_query}"

        if year_from or year_to:
            date_from = f"{year_from or 1900}0101"
            date_to = f"{year_to or 2100}1231"
            search_query = f"{search_query} AND submittedDate:[{date_from} TO {date_to}]"

        if sort_by == "date":
            sort_param = "submittedDate"
            sort_order = "descending"
        else:
            sort_param = "relevance"
            sort_order = "descending"

        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
            "sortBy": sort_param,
            "sortOrder": sort_order,
        }
        root = await self._fetch_xml(params)
        if root is None:
            return []
        entries = root.findall(f"{{{ATOM_NS}}}entry")

        papers = []
        for entry in entries:
            try:
                papers.append(self._parse_entry(entry))
            except Exception:
                entry_id = entry.findtext(f"{{{ATOM_NS}}}id", default="unknown")
                logger.exception("Failed to parse arXiv entry: %s", entry_id)
                continue

        return papers

    async def fetch_new_since(
        self,
        since: datetime,
        topics: list[TopicRef],
        limit: int = 100,
    ) -> list[PaperCreate]:
        """Fetch papers submitted after *since* that match any of the given topics.

        Uses the arXiv ``submittedDate`` range filter combined with per-topic
        title/abstract queries.  Each topic generates one API request (one per
        topic query term group) to stay within the 3 req/sec rate limit.

        Parameters
        ----------
        since : datetime
            Lower bound (exclusive) for submission date.  Must be timezone-aware.
        topics : list[TopicRef]
            Topics to include; each topic's ``query_terms`` or ``name`` is used
            to build an OR filter.  An empty list triggers a single undirected
            date-range query.
        limit : int
            Maximum total results to return (across all topics).

        Returns
        -------
        list[PaperCreate]
            Deduplicated papers newer than *since*, ordered by submission date.
        """
        # Normalise *since* to UTC, then format as arXiv date string YYYYMMDDHHMM
        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        since_str = since_utc.strftime("%Y%m%d%H%M")
        # PI-EDGE-014: arXiv submittedDate upper bound.  The API does not support
        # an open-ended "TO ]" range; use a far-future sentinel (year 2999) instead
        # of the ambiguous 99999999 which arXiv may reject as an invalid date.
        date_filter = f"submittedDate:[{since_str} TO 29991231]"

        if not topics:
            # No topic filter — just poll by date
            topic_queries = [""]
        else:
            topic_queries = []
            for topic in topics:
                terms = topic.query_terms if topic.query_terms else [topic.name]
                parts = [f'(ti:"{t}" OR abs:"{t}")' for t in terms]
                topic_queries.append(" OR ".join(parts))

        seen_ids: set[str] = set()
        papers: list[PaperCreate] = []

        per_topic = max(1, limit // max(len(topic_queries), 1))

        for topic_q in topic_queries:
            if len(papers) >= limit:
                break
            if topic_q:
                search_query = f"({topic_q}) AND {date_filter}"
            else:
                search_query = date_filter
            params = {
                "search_query": search_query,
                "start": 0,
                "max_results": per_topic,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            try:
                root = await self._fetch_xml(params)
            except Exception:
                logger.warning("arXiv fetch_new_since failed for query: %s", search_query)
                continue
            if root is None:
                logger.warning("arXiv fetch_new_since returned no data for query: %s", search_query)
                continue

            entries = root.findall(f"{{{ATOM_NS}}}entry")
            for entry in entries:
                try:
                    paper = self._parse_entry(entry)
                except Exception:
                    entry_id = entry.findtext(f"{{{ATOM_NS}}}id", default="unknown")
                    logger.exception("Failed to parse arXiv entry: %s", entry_id)
                    continue

                if paper.external_id in seen_ids:
                    continue
                seen_ids.add(paper.external_id)
                papers.append(paper)
                if len(papers) >= limit:
                    break

        return papers

    async def fetch_by_id(self, external_id: str) -> PaperCreate | None:
        """Fetch a single paper by arXiv ID.

        Parameters
        ----------
        external_id : str
            arXiv ID, with or without ``"arxiv:"`` prefix (e.g., ``"2301.12345"``).

        Returns
        -------
        PaperCreate | None
            The paper if found.
        """
        arxiv_id = external_id.removeprefix("arxiv:")
        params = {"id_list": arxiv_id}
        root = await self._fetch_xml(params)
        if root is None:
            return None
        entries = root.findall(f"{{{ATOM_NS}}}entry")

        if not entries:
            return None

        # arXiv returns an entry even for invalid IDs — check for error title
        first = entries[0]
        if first.findtext(f"{{{ATOM_NS}}}title", default="").strip() == "Error":
            return None

        return self._parse_entry(first)
