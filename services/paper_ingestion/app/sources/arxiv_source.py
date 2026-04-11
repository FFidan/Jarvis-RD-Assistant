"""arXiv paper source implementation.

Uses the arXiv Atom API (https://export.arxiv.org/api/query).
Rate limit: 3 requests/second per arXiv Terms of Use.
"""

import asyncio
import logging
import re
from datetime import date
from typing import Any
from defusedxml import ElementTree as ET

import httpx

from app.models import PaperCreate, PaperSourceConfig, SourceType
from app.sources.base import PaperSource
from app.sources.registry import register_source

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
        self._last_request_time: float = 0.0
        self._rate_lock = asyncio.Lock()

    async def _rate_limit(self) -> None:
        """Enforce arXiv 3 req/sec rate limit."""
        async with self._rate_lock:
            now = asyncio.get_running_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < RATE_LIMIT_DELAY:
                await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
            self._last_request_time = asyncio.get_running_loop().time()

    async def _fetch_xml(self, params: dict) -> Any:
        """Make a rate-limited GET request to the arXiv API and parse XML.

        Parameters
        ----------
        params : dict
            Query parameters for the arXiv API.

        Returns
        -------
        Element
            Parsed XML root element.

        Raises
        ------
        httpx.HTTPStatusError
            If the request returns a non-2xx status.
        """
        await self._rate_limit()
        response = await self.http_client.get(ARXIV_API_URL, params=params, timeout=30.0)
        response.raise_for_status()
        return ET.fromstring(response.text)

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
            cat.get("term", "")
            for cat in entry.findall(f"{{{ARXIV_NS}}}primary_category")
        ]
        categories.extend(
            cat.get("term", "")
            for cat in entry.findall(f"{{{ATOM_NS}}}category")
        )

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
        safe = raw_query.replace('"', '').strip()
        if not safe:
            return f"all:{raw_query}"

        # Search title OR abstract with the full phrase — much more relevant than all:
        return f'(ti:"{safe}" OR abs:"{safe}")'

    async def search(self, query: str, max_results: int = 10) -> list[PaperCreate]:
        """Search arXiv for papers matching the query.

        Parameters
        ----------
        query : str
            arXiv search query (supports arXiv query syntax like
            ``"cat:cs.AI AND ti:transformer"``).
        max_results : int
            Maximum results to return.

        Returns
        -------
        list[PaperCreate]
            Papers parsed from arXiv API response.
        """
        search_query = self._build_search_query(query)
        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        root = await self._fetch_xml(params)
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
        entries = root.findall(f"{{{ATOM_NS}}}entry")

        if not entries:
            return None

        # arXiv returns an entry even for invalid IDs — check for error title
        first = entries[0]
        if first.findtext(f"{{{ATOM_NS}}}title", default="").strip() == "Error":
            return None

        return self._parse_entry(first)
