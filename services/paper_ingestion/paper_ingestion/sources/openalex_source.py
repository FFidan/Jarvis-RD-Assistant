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
import time as _time
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from jarvis_common.event_log import log_event
from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter, SourceRateLimiter

from paper_ingestion.models import PaperCreate, PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from paper_ingestion.sources.base import PaperSource, SourceQuery, _enforce_startup_grace
from paper_ingestion.sources.registry import register_source

logger = logging.getLogger(__name__)

OPENALEX_API_URL = "https://api.openalex.org/works"


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

    def __init__(
        self,
        config: PaperSourceConfig,
        http_client: httpx.AsyncClient,
        db_pool: Any = None,
    ) -> None:
        super().__init__(config, http_client, db_pool)
        cfg_key = config.config.get("api_key") if config.config else None
        self._api_key: str | None = cfg_key or os.environ.get("OPENALEX_API_KEY")
        self._email: str = os.environ.get("OPENALEX_EMAIL", "")
        self._missing_key_warned = False
        # rate: ~9 req/s (polite-pool target ≤10 req/s)
        self._rate_limiter = SourceRateLimiter(rate_per_second=1.0 / 0.11)

    def consolidate_topics(self, topics: list[TopicRef]) -> list[SourceQuery]:
        """Merge all topics into a single OpenAlex query.

        For topics that have OpenAlex concept IDs in their metadata, builds a
        ``concepts.id:C123|C456`` filter.  Topics without concept IDs fall back
        to the existing per-term text search behaviour (all terms ORed together).

        Returns
        -------
        list[SourceQuery]
            A single :class:`SourceQuery` covering all topics.
        """
        if not topics:
            return []

        concept_ids: list[str] = []
        text_terms: list[str] = []

        for topic in topics:
            # Collect OpenAlex concept IDs stored in topic metadata if present.
            meta = getattr(topic, "metadata", None) or {}
            cid = meta.get("openalex_concept_id") or meta.get("concept_id")
            if cid:
                # Normalise: strip URL prefix → plain Cxxx ID
                cid_clean = str(cid).removeprefix("https://openalex.org/")
                concept_ids.append(cid_clean)
            else:
                terms = topic.query_terms if topic.query_terms else [topic.name]
                text_terms.extend(terms)

        extra: dict[str, Any] = {}
        if concept_ids:
            extra["concept_filter"] = "|".join(concept_ids)
        if text_terms:
            extra["text_search"] = " OR ".join(text_terms)

        return [SourceQuery(topics=list(topics), extra_params=extra)]

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
        better cache behaviour).  ``OPENALEX_API_KEY``, when set, is sent as
        the ``api_key`` query parameter (the correct OpenAlex auth mechanism —
        Bearer header auth is not supported by the OpenAlex API).
        """
        params: dict = {}
        if self._email:
            params["mailto"] = self._email
        if self._api_key:
            params["api_key"] = self._api_key
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

        # PI-EDGE-007: Validate pdf_url scheme + hostname against the SSRF allowlist
        # before storing it — OpenAlex can return arbitrary third-party PDF URLs.
        if pdf_url is not None:
            _parsed = urlparse(pdf_url)
            hostname = _parsed.hostname or ""
            if _parsed.scheme not in ("http", "https") or hostname not in ALLOWED_PDF_DOMAINS:
                logger.info(
                    "OpenAlex: pdf_url %r for work %s rejected "
                    "(scheme=%r, hostname=%r not in ALLOWED_PDF_DOMAINS); discarding pdf_url",
                    pdf_url,
                    external_id,
                    _parsed.scheme,
                    _parsed.hostname,
                )
                pdf_url = None

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
        try:
            response = await self.http_client.get(OPENALEX_API_URL, params=params, timeout=30.0)
            if response.status_code in (429, 500, 502, 503, 504):
                self._record_transient_poll_diagnostic(response)
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
        try:
            response = await self.http_client.get(url, params=params, timeout=30.0)
            if response.status_code == 404:
                return None
            if response.status_code in (429, 500, 502, 503, 504):
                self._record_transient_poll_diagnostic(response)
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
        user_id: int | None = None,
    ) -> list[PaperCreate]:
        """Fetch OpenAlex works published after *since* relevant to the given topics.

        Consolidates all topics into a single API query via
        :meth:`consolidate_topics` (concept-ID filter when available, falling
        back to free-text search).  Wires :class:`PersistentSourceRateLimiter`
        when ``db_pool`` is set and records each attempt in ``source_run_history``.

        Parameters
        ----------
        since : datetime
            Lower bound for ``publication_date`` (inclusive; OpenAlex rounds
            to the day).
        topics : list[TopicRef]
            Topics to include; consolidated into one query.
            An empty list triggers a single date-only query.
        limit : int
            Maximum total papers to return.

        Returns
        -------
        list[PaperCreate]
            Deduplicated works newer than *since*.  Returns ``[]`` if neither
            ``OPENALEX_EMAIL`` nor ``OPENALEX_API_KEY`` is configured, or on
            HTTP errors.
        """
        if not self._check_api_key():
            return []

        # Startup grace — see ArxivSource.fetch_new_since for details.
        grace = getattr(getattr(self.config, "pulse", None), "startup_grace_seconds", 0.0)
        await _enforce_startup_grace(grace)

        # Persistent rate limiter (no-op when db_pool is None).
        p_limiter: PersistentSourceRateLimiter | None = None
        if self.db_pool is not None:
            p_limiter = PersistentSourceRateLimiter(
                source_type="openalex",
                user_id=user_id,
                min_interval_seconds=0.11,
                db_pool=self.db_pool,
                fallback=self._rate_limiter,
            )

        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        date_str = since_utc.strftime("%Y-%m-%d")
        date_filter = f"from_publication_date:{date_str}"

        if not topics:
            consolidated: list[SourceQuery] = [SourceQuery(topics=[], extra_params={})]
        else:
            consolidated = self.consolidate_topics(topics)

        seen_ids: set[str] = set()
        papers: list[PaperCreate] = []
        per_q = max(1, limit // max(len(consolidated), 1))

        for sq in consolidated:
            if len(papers) >= limit:
                break

            extra_params = sq.extra_params

            # Build filter param: start with date, add concept IDs if present.
            filters: list[str] = [date_filter]
            concept_filter = extra_params.get("concept_filter")
            if concept_filter:
                filters.append(f"concepts.id:{concept_filter}")
            filter_str = ",".join(filters)

            params = self._build_params({"filter": filter_str, "per-page": min(per_q, 200)})

            # Apply text search for topics without concept IDs.
            text_search = extra_params.get("text_search")
            if text_search:
                params["search"] = text_search

            started_at = _time.monotonic()

            if p_limiter is not None:
                await p_limiter.acquire()
            else:
                await self._rate_limit()

            try:
                response = await self.http_client.get(OPENALEX_API_URL, params=params, timeout=30.0)
                if response.status_code in (429, 500, 502, 503, 504):
                    self._record_transient_poll_diagnostic(response)
                    logger.warning(
                        "OpenAlex fetch_new_since returned %d; skipping query",
                        response.status_code,
                    )
                    p_status = "rate_limit" if response.status_code == 429 else "error"
                    retry_after = self._retry_after_seconds(response)
                    if p_limiter is not None:
                        await p_limiter.update_last_request(p_status, retry_after_s=retry_after)
                    await self._insert_run_history(
                        started_at=started_at,
                        status=p_status,
                        candidate_count=0,
                        duration_ms=int((_time.monotonic() - started_at) * 1000),
                        user_id=user_id,
                    )
                    if self.db_pool is not None:
                        try:
                            if p_status == "rate_limit":
                                await log_event(
                                    pool=self.db_pool,
                                    level="warning",
                                    category="source",
                                    source="openalex",
                                    message="rate_limited",
                                    context={
                                        "http_status": response.status_code,
                                        "retry_after_s": retry_after,
                                    },
                                )
                            else:
                                await log_event(
                                    pool=self.db_pool,
                                    level="error",
                                    category="source",
                                    source="openalex",
                                    message="fetch_failed",
                                    context={
                                        "http_status": response.status_code,
                                        "exception": None,
                                    },
                                )
                        except Exception:
                            pass
                    continue
                response.raise_for_status()
                data = response.json()
                self._clear_poll_diagnostic()
            except httpx.HTTPError as exc:
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
                logger.warning("OpenAlex fetch_new_since failed: %s", exc)
                if p_limiter is not None:
                    await p_limiter.update_last_request("error")
                await self._insert_run_history(
                    started_at=started_at,
                    status="error",
                    candidate_count=0,
                    duration_ms=int((_time.monotonic() - started_at) * 1000),
                    user_id=user_id,
                )
                if self.db_pool is not None:
                    try:
                        _exc_status = getattr(getattr(exc, "response", None), "status_code", None)
                        await log_event(
                            pool=self.db_pool,
                            level="error",
                            category="source",
                            source="openalex",
                            message="fetch_failed",
                            context={"http_status": _exc_status, "exception": repr(exc)[:300]},
                        )
                    except Exception:
                        pass
                continue

            candidate_count = 0
            for work in data.get("results") or []:
                raw_id = work.get("id", "")
                oa_id = raw_id.removeprefix("https://openalex.org/")
                if oa_id in seen_ids:
                    continue
                seen_ids.add(oa_id)
                try:
                    papers.append(self._parse_work(work))
                    candidate_count += 1
                except Exception:
                    logger.exception("OpenAlex: failed to parse work %s", oa_id)
                if len(papers) >= limit:
                    break

            duration_ms = int((_time.monotonic() - started_at) * 1000)
            if p_limiter is not None:
                await p_limiter.update_last_request("ok")
            await self._insert_run_history(
                started_at=started_at,
                status="ok",
                candidate_count=candidate_count,
                duration_ms=duration_ms,
                user_id=user_id,
            )
            if self.db_pool is not None:
                try:
                    await log_event(
                        pool=self.db_pool,
                        level="info",
                        category="source",
                        source="openalex",
                        message="fetch_succeeded",
                        context={
                            "http_status": 200,
                            "papers_fetched": candidate_count,
                            "query_count": len(consolidated),
                        },
                    )
                except Exception:
                    pass

        return papers

    async def _insert_run_history(
        self,
        *,
        started_at: float,
        status: str,
        candidate_count: int,
        duration_ms: int,
        user_id: int | None = None,
    ) -> None:
        """Insert a row into ``source_run_history`` if ``db_pool`` is available."""
        if self.db_pool is None:
            return
        import datetime as _dt

        now_utc = _dt.datetime.now(tz=_dt.UTC)
        started_utc = now_utc - _dt.timedelta(milliseconds=duration_ms)
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO source_run_history
                        (user_id, source_type, started_at, finished_at,
                         status, candidate_count, duration_ms, detail)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                    """,
                    user_id,
                    "openalex",
                    started_utc,
                    now_utc,
                    status,
                    candidate_count,
                    duration_ms,
                    "{}",
                )
        except Exception as exc:
            logger.warning("OpenAlex: failed to insert source_run_history: %s", exc)
