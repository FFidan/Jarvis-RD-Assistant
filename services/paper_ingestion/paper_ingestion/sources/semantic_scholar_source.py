"""Semantic Scholar paper source implementation.

Uses the Semantic Scholar Academic Graph API
(https://api.semanticscholar.org/graph/v1).
Rate limit: 1 request/second on the free tier.
"""

import logging
import os
from datetime import date
from typing import Any
from urllib.parse import quote as _url_quote

import httpx
from jarvis_common.source_rate_limiter import SourceRateLimiter
from jarvis_common.text_utils import author_matches

from paper_ingestion.models import PaperCreate, PaperSourceConfig, SourceType
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.registry import register_source

logger = logging.getLogger(__name__)

S2_API_URL = "https://api.semanticscholar.org/graph/v1"
S2_RECOMMENDATIONS_URL = "https://api.semanticscholar.org/recommendations/v1"
RATE_LIMIT_DELAY = 1.05  # seconds between requests (free tier: 1 req/sec)
S2_FIELDS = (
    "paperId,externalIds,title,authors,authors.authorId,abstract,year,"
    "publicationDate,url,citationCount,openAccessPdf,tldr"
)


@register_source
class SemanticScholarSource(PaperSource):
    """Semantic Scholar Academic Graph API paper source.

    Attributes
    ----------
    source_type : str
        Always ``"semantic_scholar"``.
    """

    source_type = "semantic_scholar"

    def __init__(self, config: PaperSourceConfig, http_client: httpx.AsyncClient) -> None:
        super().__init__(config, http_client)
        # Optional API key for higher rate limits (config overrides env var)
        cfg_key = config.config.get("api_key") if config.config else None
        self._api_key: str | None = cfg_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        # rate: 1/RATE_LIMIT_DELAY req/s (S2 free-tier 1 req/s)
        self._rate_limiter = SourceRateLimiter(rate_per_second=1.0 / RATE_LIMIT_DELAY)

    async def _rate_limit(self) -> None:
        """Enforce Semantic Scholar free-tier rate limit (1 req/sec)."""
        await self._rate_limiter.acquire()

    def _build_headers(self) -> dict[str, str]:
        """Build request headers, including API key if configured."""
        headers: dict[str, str] = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    async def _fetch_json(self, path: str, params: dict | None = None) -> dict[str, Any]:
        """Make a rate-limited GET request to the S2 API and return JSON.

        Parameters
        ----------
        path : str
            API path relative to the base URL (e.g., ``/paper/search``).
        params : dict | None
            Query parameters.

        Returns
        -------
        dict
            Parsed JSON response.

        Raises
        ------
        httpx.HTTPStatusError
            If the request returns a non-2xx status.
        """
        await self._rate_limit()
        url = f"{S2_API_URL}{path}"
        response = await self.http_client.get(
            url, params=params, headers=self._build_headers(), timeout=30.0
        )
        response.raise_for_status()
        return response.json()

    def _parse_paper(self, data: dict[str, Any]) -> PaperCreate:
        """Convert a Semantic Scholar paper JSON object to a PaperCreate model.

        Parameters
        ----------
        data : dict
            A single paper object from the S2 API response.

        Returns
        -------
        PaperCreate
            Paper with all metadata from S2 API (never LLM-generated).
        """
        paper_id = data.get("paperId", "")
        external_ids = data.get("externalIds") or {}

        title = (data.get("title") or "").strip()

        authors = [
            author.get("name", "") for author in (data.get("authors") or []) if author.get("name")
        ]

        abstract = (data.get("abstract") or "").strip()

        # Parse publication date: prefer ISO date string, fallback to year
        published_date: date | None = None
        pub_date_str = data.get("publicationDate")
        if pub_date_str:
            try:
                published_date = date.fromisoformat(pub_date_str)
            except ValueError:
                logger.warning(
                    "Invalid publicationDate for S2 paper %s: %s", paper_id, pub_date_str
                )  # noqa: E501
        if published_date is None and data.get("year"):
            published_date = date(data["year"], 1, 1)

        # PDF URL from openAccessPdf
        pdf_url: str | None = None
        open_access = data.get("openAccessPdf")
        if open_access and isinstance(open_access, dict):
            pdf_url = open_access.get("url")
        if pdf_url is not None and not pdf_url.strip():
            pdf_url = None  # Treat empty/whitespace-only as missing

        url = data.get("url") or f"https://www.semanticscholar.org/paper/{paper_id}"

        citation_count = data.get("citationCount") or 0

        # Build metadata
        metadata: dict[str, Any] = {"s2_id": paper_id}
        if external_ids.get("ArXiv"):
            metadata["arxiv_id"] = external_ids["ArXiv"]
        if external_ids.get("DOI"):
            metadata["doi"] = external_ids["DOI"]

        # TLDR
        tldr_data = data.get("tldr")
        if tldr_data and isinstance(tldr_data, dict):
            metadata["s2_tldr"] = tldr_data.get("text", "")

        # Author IDs
        s2_author_ids = [
            {"name": a.get("name", ""), "authorId": a.get("authorId")}
            for a in (data.get("authors") or [])
            if a.get("authorId")
        ]
        if s2_author_ids:
            metadata["s2_author_ids"] = s2_author_ids

        return PaperCreate(
            external_id=f"s2:{paper_id}",
            source_type=SourceType.SEMANTIC_SCHOLAR,
            title=title,
            authors=authors,
            abstract=abstract,
            published_date=published_date,
            url=url,
            pdf_url=pdf_url,
            citation_count=citation_count,
            metadata=metadata,
        )

    async def search(
        self,
        query: str,
        max_results: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        sort_by: str = "relevance",
        author: str | None = None,
    ) -> list[PaperCreate]:
        """Search Semantic Scholar for papers matching the query.

        Parameters
        ----------
        query : str
            Free-text search query.
        max_results : int
            Maximum results to return.
        year_from : int | None
            Filter to papers published from this year (if year_to also set,
            uses ``year={year_from}-{year_to}`` S2 range syntax).
        year_to : int | None
            Filter to papers published up to this year (used with year_from).
        sort_by : str
            Sort order: ``"relevance"`` (default) or ``"date"``.
            Note: S2 API does not support sort-by-date natively; this param
            is accepted but has no effect on S2 results.
        author : str | None
            Author name filter. Note: S2 API does not support author-name
            search natively; this param is accepted but has no effect.

        Returns
        -------
        list[PaperCreate]
            Papers parsed from S2 API response.
        """
        params: dict = {
            "query": query,
            "limit": min(max_results, 100),  # S2 API max is 100
            "fields": S2_FIELDS,
        }

        if year_from and year_to:
            params["year"] = f"{year_from}-{year_to}"
        elif year_from:
            params["year"] = f"{year_from}-"
        elif year_to:
            params["year"] = f"-{year_to}"

        if sort_by == "date":
            logger.debug("S2 search: sort_by='date' not natively supported by S2 API")

        response_data = await self._fetch_json("/paper/search", params=params)

        papers = []
        for item in response_data.get("data") or []:
            try:
                papers.append(self._parse_paper(item))
            except Exception:
                item_id = item.get("paperId", "unknown")
                logger.exception("Failed to parse S2 paper: %s", item_id)
                continue

        # Client-side author filter: S2 API does not support author-name search natively.
        # Keep papers where the filter term matches any author in the result.
        if author:
            filtered = [p for p in papers if any(author_matches(author, a) for a in p.authors)]
            logger.debug(
                "S2 author filter '%s': %d/%d papers kept",
                author,
                len(filtered),
                len(papers),
            )
            papers = filtered

        return papers

    async def fetch_by_id(self, external_id: str) -> PaperCreate | None:
        """Fetch a single paper by Semantic Scholar paper ID.

        Parameters
        ----------
        external_id : str
            S2 paper ID, with or without ``"s2:"`` prefix.
            Also accepts arXiv IDs (``"arXiv:2301.12345"``) or DOIs.

        Returns
        -------
        PaperCreate | None
            The paper if found, None otherwise.
        """
        paper_id = external_id.removeprefix("s2:")
        params = {"fields": S2_FIELDS}
        try:
            data = await self._fetch_json(f"/paper/{_url_quote(paper_id, safe='')}", params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

        return self._parse_paper(data)

    async def fetch_citations(self, paper_id: str, limit: int = 100) -> list[dict]:
        """Fetch papers that cite the given paper.

        Parameters
        ----------
        paper_id : str
            Semantic Scholar paper ID.
        limit : int
            Maximum citations to return (S2 max is 1000).

        Returns
        -------
        list[dict]
            List of citation data dicts from S2 API.
        """
        params = {
            "fields": "paperId,externalIds,title,authors,year,citationCount,contexts,isInfluential,intents",  # noqa: E501
            "limit": min(limit, 1000),
        }
        data = await self._fetch_json(
            f"/paper/{_url_quote(paper_id, safe='')}/citations", params=params
        )
        return data.get("data", [])

    async def get_recommendations(
        self,
        positive_seeds: list[str],
        negative_seeds: list[str] | None = None,
        limit: int = 50,
    ) -> list[PaperCreate]:
        """Recommend papers similar to positive seeds using the S2 Recommendations API.

        Primary path (API key present): ``POST /recommendations/v1/papers`` with
        multi-seed body — uses the full positive/negative seed list.

        Fallback path (no API key): loops over the top-3 positive seeds and calls
        ``GET /recommendations/v1/papers/forpaper/{id}`` for each, then dedupes by
        S2 paper ID and trims to ``limit``.

        Parameters
        ----------
        positive_seeds : list[str]
            S2 paper IDs to use as positive examples.
        negative_seeds : list[str] | None
            Optional S2 paper IDs to steer away from.
        limit : int
            Maximum number of recommendations to return.

        Returns
        -------
        list[PaperCreate]
            Recommended papers parsed from the S2 API. Returns ``[]`` on 429/5xx.
        """
        if not positive_seeds:
            return []

        rec_fields = (
            "paperId,externalIds,title,authors,authors.authorId,abstract,year,"
            "publicationDate,url,citationCount,openAccessPdf,tldr"
        )

        if self._api_key:
            # Primary path: multi-seed POST endpoint
            body: dict[str, Any] = {
                "positivePaperIds": positive_seeds,
                "negativePaperIds": negative_seeds or [],
            }
            params = {"limit": min(limit, 500), "fields": rec_fields}
            try:
                await self._rate_limit()
                response = await self.http_client.post(
                    f"{S2_RECOMMENDATIONS_URL}/papers",
                    json=body,
                    params=params,
                    headers=self._build_headers(),
                    timeout=30.0,
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "S2 recommendations POST returned %d; returning empty list",
                        response.status_code,
                    )
                    return []
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                logger.warning("S2 recommendations POST failed: %s", exc)
                return []

            papers = []
            for item in data.get("recommendedPapers") or []:
                try:
                    papers.append(self._parse_paper(item))
                except Exception:
                    logger.exception("Failed to parse S2 recommendation: %s", item.get("paperId"))
            return papers[:limit]

        # Fallback: per-paper GET loop over top-3 seeds
        seen_ids: set[str] = set()
        papers = []
        per_seed = max(17, limit)  # over-fetch so trimming works
        for seed_id in positive_seeds[:3]:
            params = {"limit": per_seed, "fields": rec_fields}
            try:
                await self._rate_limit()
                response = await self.http_client.get(
                    f"{S2_RECOMMENDATIONS_URL}/papers/forpaper/{_url_quote(seed_id, safe='')}",
                    params=params,
                    headers=self._build_headers(),
                    timeout=30.0,
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "S2 forpaper/%s returned %d; skipping this seed",
                        seed_id,
                        response.status_code,
                    )
                    continue
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                logger.warning("S2 forpaper/%s failed: %s; skipping", seed_id, exc)
                continue

            for item in data.get("recommendedPapers") or []:
                pid = item.get("paperId", "")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                try:
                    papers.append(self._parse_paper(item))
                except Exception:
                    logger.exception("Failed to parse S2 recommendation: %s", pid)
                if len(papers) >= limit:
                    break
            if len(papers) >= limit:
                break

        return papers[:limit]

    async def fetch_references(self, paper_id: str, limit: int = 100) -> list[dict]:
        """Fetch papers cited BY the given paper.

        Parameters
        ----------
        paper_id : str
            Semantic Scholar paper ID.
        limit : int
            Maximum references to return (S2 max is 1000).

        Returns
        -------
        list[dict]
            List of reference data dicts from S2 API.
        """
        params = {
            "fields": "paperId,externalIds,title,authors,year,citationCount,contexts,isInfluential,intents",  # noqa: E501
            "limit": min(limit, 1000),
        }
        data = await self._fetch_json(
            f"/paper/{_url_quote(paper_id, safe='')}/references", params=params
        )
        return data.get("data", [])
