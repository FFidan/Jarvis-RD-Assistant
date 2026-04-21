"""OpenAlex paper source implementation.

Uses the OpenAlex Works API (https://api.openalex.org/works).

As of February 2026 OpenAlex requires an API key (polite pool token) for
non-trivial query volumes.  Pass your key via the ``OPENALEX_API_KEY``
environment variable or the ``api_key`` field in the source config.  If no
key is present, all methods return ``[]`` and log once at INFO level — the
source degrades gracefully rather than raising.

Abstract reconstruction note
------------------------------
OpenAlex stores paper abstracts as an *inverted index*: a dict mapping each
word to the list of positions it occupies in the abstract, e.g.::

    {"The": [0], "model": [1, 5], "learns": [2], ...}

``_reconstruct_abstract`` reverses this into a plain string by placing each
word at its positions in an output list, then joining with spaces.

Rate limiting
-------------
OpenAlex recommends ≤10 requests/second for polite-pool users.  This plugin
enforces ~9 req/s (0.11 s interval) via an asyncio.Lock-based rate limiter
shared across all calls on the same instance.
"""

import logging
import os
from datetime import UTC, date, datetime

import httpx
from jarvis_common.source_rate_limiter import SourceRateLimiter

from paper_ingestion.models import PaperCreate, PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.registry import register_source

logger = logging.getLogger(__name__)

OPENALEX_API_URL = "https://api.openalex.org/works"
_OPENALEX_MISSING_KEY_LOGGED: set[int] = set()  # track per-instance to avoid spam


def _reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Reconstruct an abstract string from an OpenAlex abstract_inverted_index.

    OpenAlex stores abstracts as a word→positions dict (inverted index) to
    reduce storage costs.  This helper reverses the index into a plain text
    string by placing each word at all its stated positions and joining with
    spaces.

    Parameters
    ----------
    inverted_index : dict | None
        Mapping of word → list[int] as returned by OpenAlex, or ``None`` if
        the abstract is unavailable.

    Returns
    -------
    str | None
        Reconstructed abstract text, or ``None`` if the index is absent/empty.

    Examples
    --------
    >>> _reconstruct_abstract({"Hello": [0], "world": [1]})
    'Hello world'
    >>> _reconstruct_abstract(None) is None
    True
    """
    if not inverted_index:
        return None

    # Find the length of the abstract (max position + 1)
    max_pos = 0
    for positions in inverted_index.values():
        for pos in positions:
            if pos > max_pos:
                max_pos = pos

    tokens: list[str] = [""] * (max_pos + 1)
    for word, positions in inverted_index.items():
        for pos in positions:
            if 0 <= pos < len(tokens):
                tokens[pos] = word

    return " ".join(tokens).strip() or None


@register_source
class OpenAlexSource(PaperSource):
    """OpenAlex Works API paper source.

    Attributes
    ----------
    source_type : str
        Always ``"openalex"``.
    """

    source_type = "openalex"

    def __init__(self, config: PaperSourceConfig, http_client: httpx.AsyncClient) -> None:
        super().__init__(config, http_client)
        cfg_key = config.config.get("api_key") if config.config else None
        self._api_key: str | None = cfg_key or os.environ.get("OPENALEX_API_KEY")
        self._email: str = os.environ.get("OPENALEX_EMAIL", "")
        self._missing_key_warned = False
        # rate: ~9 req/s (polite-pool target ≤10 req/s)
        self._rate_limiter = SourceRateLimiter(rate_per_second=1.0 / 0.11)

    async def _rate_limit(self) -> None:
        """Enforce OpenAlex polite-pool rate limit: ≤10 req/s (~9 req/s target)."""
        await self._rate_limiter.acquire()

    def _check_api_key(self) -> bool:
        """Return True if the source has at least an email or API key configured.

        Logs once at INFO level when neither ``OPENALEX_EMAIL`` nor
        ``OPENALEX_API_KEY`` is set, then returns ``False`` so callers can
        degrade gracefully.
        """
        if self._api_key or self._email:
            return True
        if not self._missing_key_warned:
            logger.info(
                "OpenAlex source: neither OPENALEX_EMAIL nor OPENALEX_API_KEY is set. "
                "Set OPENALEX_EMAIL for polite-pool access (recommended) or "
                "OPENALEX_API_KEY for authenticated access. "
                "Returning empty results."
            )
            self._missing_key_warned = True
        return False

    def _build_params(self, extra: dict | None = None) -> dict:
        """Build base query params including the polite-pool ``mailto`` token.

        ``OPENALEX_EMAIL`` is sent as the ``mailto`` query parameter which
        places this client in OpenAlex's polite pool (higher rate limits,
        better cache behaviour).  ``OPENALEX_API_KEY``, when set, is sent as a
        Bearer token via the ``Authorization`` header (handled separately in
        each request) — it is not mixed into the query params.
        """
        params: dict = {}
        if self._email:
            params["mailto"] = self._email
        if extra:
            params.update(extra)
        return params

    def _parse_work(self, work: dict) -> PaperCreate:
        """Convert an OpenAlex Work JSON object into a PaperCreate model.

        Parameters
        ----------
        work : dict
            A single Work object from the OpenAlex API response.

        Returns
        -------
        PaperCreate
            Paper with metadata populated entirely from the OpenAlex API.
        """
        raw_id: str = work.get("id", "")
        # Strip the URL prefix → "W12345678"
        external_id = raw_id.removeprefix("https://openalex.org/")

        title: str = (work.get("title") or work.get("display_name") or "").strip()

        authors = [
            auth["author"]["display_name"]
            for auth in (work.get("authorships") or [])
            if auth.get("author", {}).get("display_name")
        ]

        abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

        doi_raw: str | None = work.get("doi")
        doi: str | None = None
        if doi_raw:
            doi = doi_raw.removeprefix("https://doi.org/")

        pub_date: date | None = None
        pub_date_str: str | None = work.get("publication_date")
        if pub_date_str:
            try:
                pub_date = date.fromisoformat(pub_date_str[:10])
            except ValueError:
                logger.warning(
                    "OpenAlex: invalid publication_date %r for work %s",
                    pub_date_str,
                    external_id,
                )

        pdf_url: str | None = None
        primary_location = work.get("primary_location")
        if primary_location and isinstance(primary_location, dict):
            pdf_url = primary_location.get("pdf_url") or None

        # Build a usable URL for this work
        url = raw_id if raw_id.startswith("http") else f"https://openalex.org/{external_id}"

        metadata: dict = {"openalex_id": external_id}
        if doi:
            metadata["doi"] = doi

        return PaperCreate(
            external_id=f"openalex:{external_id}",
            source_type=SourceType.OPENALEX,
            title=title,
            authors=authors,
            abstract=abstract,
            published_date=pub_date,
            url=url,
            pdf_url=pdf_url,
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
        """Search OpenAlex for papers matching the query.

        Parameters
        ----------
        query : str
            Free-text search query forwarded to the OpenAlex ``search`` param.
        max_results : int
            Maximum number of results to return (capped at 200 per API call).
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
            Papers parsed from the OpenAlex response.  Returns ``[]`` if
            neither ``OPENALEX_EMAIL`` nor ``OPENALEX_API_KEY`` is configured,
            or on HTTP 429/5xx.
        """
        if not self._check_api_key():
            return []

        await self._rate_limit()

        extra: dict = {"search": query, "per-page": min(max_results, 200)}

        # Build filter string
        filters: list[str] = []
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")
        if author:
            filters.append(f"author.display_name.search:{author}")
        if filters:
            extra["filter"] = ",".join(filters)

        if sort_by == "date":
            extra["sort"] = "publication_date:desc"

        params = self._build_params(extra)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            response = await self.http_client.get(
                OPENALEX_API_URL, params=params, headers=headers, timeout=30.0
            )
            if response.status_code in (429, 500, 502, 503, 504):
                logger.warning(
                    "OpenAlex search returned %d; returning empty list",
                    response.status_code,
                )
                return []
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("OpenAlex search failed: %s", exc)
            return []

        papers = []
        for work in data.get("results") or []:
            try:
                papers.append(self._parse_work(work))
            except Exception:
                logger.exception("OpenAlex: failed to parse work %s", work.get("id"))
        return papers

    async def fetch_by_id(self, external_id: str) -> PaperCreate | None:
        """Fetch a single OpenAlex work by ID or DOI.

        Parameters
        ----------
        external_id : str
            OpenAlex work ID (``W12345``) or DOI (``doi:10.xxx/yyy``), with or
            without the ``openalex:`` prefix.

        Returns
        -------
        PaperCreate | None
            The paper if found, ``None`` on 404.  Returns ``None`` on error.
        """
        if not self._check_api_key():
            return None

        # Normalise: strip openalex: prefix if present
        oa_id = external_id.removeprefix("openalex:")
        # If it looks like a DOI, format as "doi:10.xxx/yyy"
        if oa_id.startswith("10.") or oa_id.startswith("doi:"):
            oa_id = oa_id if oa_id.startswith("doi:") else f"doi:{oa_id}"

        await self._rate_limit()
        url = f"{OPENALEX_API_URL}/{oa_id}"
        params = self._build_params()
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            response = await self.http_client.get(url, params=params, headers=headers, timeout=30.0)
            if response.status_code == 404:
                return None
            if response.status_code in (429, 500, 502, 503, 504):
                logger.warning("OpenAlex fetch_by_id %s returned %d", oa_id, response.status_code)
                return None
            response.raise_for_status()
            work = response.json()
        except httpx.HTTPError as exc:
            logger.warning("OpenAlex fetch_by_id failed for %s: %s", oa_id, exc)
            return None

        try:
            return self._parse_work(work)
        except Exception:
            logger.exception("OpenAlex: failed to parse work %s", oa_id)
            return None

    async def fetch_new_since(
        self,
        since: datetime,
        topics: list[TopicRef],
        limit: int = 100,
    ) -> list[PaperCreate]:
        """Fetch OpenAlex works published after *since* relevant to the given topics.

        Uses the ``filter=from_publication_date:YYYY-MM-DD`` parameter combined
        with per-topic free-text search.  When topics are provided, one API
        request is issued per topic (search on name/query_terms); results are
        deduplicated by OpenAlex ID and trimmed to ``limit``.

        Parameters
        ----------
        since : datetime
            Lower bound for ``publication_date`` (inclusive; OpenAlex rounds
            to the day).
        topics : list[TopicRef]
            Topics to include; each generates a separate search request.
            An empty list triggers a single date-only query.
        limit : int
            Maximum total papers to return across all topic queries.

        Returns
        -------
        list[PaperCreate]
            Deduplicated works newer than *since*. Returns ``[]`` if neither
            ``OPENALEX_EMAIL`` nor ``OPENALEX_API_KEY`` is configured, or on
            HTTP errors.
        """
        if not self._check_api_key():
            return []

        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        date_str = since_utc.strftime("%Y-%m-%d")
        date_filter = f"from_publication_date:{date_str}"

        if not topics:
            queries: list[str | None] = [None]
        else:
            queries = []
            for topic in topics:
                terms = topic.query_terms if topic.query_terms else [topic.name]
                queries.append(" OR ".join(terms))

        seen_ids: set[str] = set()
        papers: list[PaperCreate] = []
        per_q = max(1, limit // max(len(queries), 1))
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        for q in queries:
            if len(papers) >= limit:
                break
            await self._rate_limit()
            params = self._build_params({"filter": date_filter, "per-page": min(per_q, 200)})
            if q:
                params["search"] = q

            try:
                response = await self.http_client.get(
                    OPENALEX_API_URL, params=params, headers=headers, timeout=30.0
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "OpenAlex fetch_new_since returned %d; skipping query",
                        response.status_code,
                    )
                    continue
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPError as exc:
                logger.warning("OpenAlex fetch_new_since failed: %s", exc)
                continue

            for work in data.get("results") or []:
                raw_id = work.get("id", "")
                oa_id = raw_id.removeprefix("https://openalex.org/")
                if oa_id in seen_ids:
                    continue
                seen_ids.add(oa_id)
                try:
                    papers.append(self._parse_work(work))
                except Exception:
                    logger.exception("OpenAlex: failed to parse work %s", oa_id)
                if len(papers) >= limit:
                    break

        return papers
