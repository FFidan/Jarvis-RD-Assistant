"""arXiv paper source implementation.

Uses the arXiv Atom API (https://export.arxiv.org/api/query).
Rate limit: repeated calls should wait at least 3 seconds per arXiv API guidance.
"""

import asyncio
import logging
import re
import time as _time
from datetime import UTC, date, datetime
from typing import Any

import httpx
from jarvis_common.event_log import log_event
from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter, SourceRateLimiter

from paper_ingestion.models import PaperCreate, PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources._xml_safe import safe_fromstring
from paper_ingestion.sources.base import PaperSource, SourceQuery, _enforce_startup_grace
from paper_ingestion.sources.registry import register_source

logger = logging.getLogger(__name__)

# arXiv Atom XML namespaces
ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"

ARXIV_API_URL = "https://export.arxiv.org/api/query"
RATE_LIMIT_DELAY = 3.0
_MAX_FETCH_ATTEMPTS = 3
_ARXIV_FIELD_PREFIX = re.compile(r"\b(ti|au|abs|co|jr|cat|rn|id|all):")
# Module-level lock ensures all ArxivSource instances share one connection slot,
# matching arXiv's "one connection at a time" policy across the whole process.
_ARXIV_REQUEST_LOCK: asyncio.Lock = asyncio.Lock()


def _retry_after_s(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


@register_source
class ArxivSource(PaperSource):
    """arXiv API paper source.

    Attributes
    ----------
    source_type : str
        Always ``"arxiv"``.
    """

    source_type = "arxiv"

    def __init__(
        self,
        config: PaperSourceConfig,
        http_client: httpx.AsyncClient,
        db_pool: Any = None,
    ) -> None:
        super().__init__(config, http_client, db_pool)
        # arXiv asks clients to wait 3 seconds between repeated calls.
        self._rate_limiter = SourceRateLimiter(rate_per_second=1.0 / RATE_LIMIT_DELAY)

    async def _rate_limit(self) -> None:
        """Enforce arXiv's conservative polling cadence."""
        await self._rate_limiter.acquire()

    def consolidate_topics(self, topics: list[TopicRef]) -> list[SourceQuery]:
        """Merge all topics into one arXiv OR-query (or 2 if > 1500 chars).

        Builds a single ``(ti:"X" OR abs:"X" OR ...)`` query from all topic
        terms, splitting into at most 2 bins when the string would exceed the
        1500-character URL ceiling.

        Returns
        -------
        list[SourceQuery]
            One or two :class:`SourceQuery` objects.
        """
        if not topics:
            return []

        def _parts_for_topic(topic: TopicRef) -> list[str]:
            terms = topic.query_terms if topic.query_terms else [topic.name]
            return [f'(ti:"{t}" OR abs:"{t}")' for t in terms]

        _cap = 1500

        def _build_query(topic_list: list[TopicRef]) -> str:
            all_parts: list[str] = []
            for t in topic_list:
                all_parts.extend(_parts_for_topic(t))
            return " OR ".join(all_parts)

        full_query = _build_query(topics)
        if len(full_query) <= _cap:
            return [SourceQuery(topics=list(topics), extra_params={"search_query": full_query})]

        # Greedy bin-pack into 2 bins.
        bin1: list[TopicRef] = []
        bin2: list[TopicRef] = []
        current_parts: list[str] = []
        for topic in topics:
            new_parts = _parts_for_topic(topic)
            tentative = " OR ".join(current_parts + new_parts)
            if len(tentative) <= _cap:
                current_parts.extend(new_parts)
                bin1.append(topic)
            else:
                bin2.append(topic)

        queries: list[SourceQuery] = []
        if bin1:
            queries.append(
                SourceQuery(
                    topics=bin1,
                    extra_params={
                        "search_query": " OR ".join(p for t in bin1 for p in _parts_for_topic(t))
                    },
                )
            )
        if bin2:
            queries.append(
                SourceQuery(
                    topics=bin2,
                    extra_params={
                        "search_query": " OR ".join(p for t in bin2 for p in _parts_for_topic(t))
                    },
                )
            )
        return queries

    async def _fetch_xml(self, params: dict) -> Any | None:
        """Rate-limited GET to the arXiv API; returns parsed XML or None on transient errors.

        Retries 429 / 5xx responses with bounded backoff so a transient arXiv
        throttle does not immediately erase the whole Pulse deck.
        """
        for attempt in range(_MAX_FETCH_ATTEMPTS):
            try:
                async with _ARXIV_REQUEST_LOCK:
                    await self._rate_limit()
                    response = await self.http_client.get(
                        ARXIV_API_URL, params=params, timeout=30.0
                    )
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < _MAX_FETCH_ATTEMPTS - 1:
                        wait_s = _retry_after_s(response.headers.get("Retry-After")) or min(
                            30.0, 3.0 * (2**attempt)
                        )
                        logger.warning(
                            "arXiv fetch returned %d; retrying in %.1fs",
                            response.status_code,
                            wait_s,
                        )
                        await asyncio.sleep(wait_s)
                        continue
                    self._record_transient_poll_diagnostic(response)
                    logger.warning(
                        "arxiv _safe_get %s returned %d; returning None",
                        ARXIV_API_URL,
                        response.status_code,
                    )
                    return None
                response.raise_for_status()
                try:
                    root = safe_fromstring(response.text)
                except Exception as exc:
                    self._set_poll_diagnostic(
                        status="error",
                        message="arXiv returned malformed XML. Try again later.",
                        status_code=200,
                        retry_after_s=None,
                        settings_hint=(
                            "Try again later; arXiv may have returned a transient invalid response."
                        ),
                    )
                    logger.warning(
                        "arxiv _safe_get %s returned malformed XML: %s", ARXIV_API_URL, exc
                    )
                    return None
                self._clear_poll_diagnostic()
                return root
            except httpx.HTTPError as exc:
                if attempt < _MAX_FETCH_ATTEMPTS - 1:
                    wait_s = min(30.0, 3.0 * (2**attempt))
                    logger.warning("arXiv fetch failed: %s; retrying in %.1fs", exc, wait_s)
                    await asyncio.sleep(wait_s)
                    continue
                response = getattr(exc, "response", None)
                if response is not None:
                    self._record_transient_poll_diagnostic(response)
                else:
                    self._set_poll_diagnostic(
                        status="error",
                        message="arXiv request failed. Check provider status and retry later.",
                        status_code=None,
                        retry_after_s=None,
                        settings_hint=None,
                    )
                logger.warning("arxiv _safe_get %s failed: %s", ARXIV_API_URL, exc)
                return None
        return None

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
        user_id: int | None = None,
    ) -> list[PaperCreate]:
        """Fetch papers submitted after *since* that match any of the given topics.

        Uses the arXiv ``submittedDate`` range filter combined with consolidated
        topic queries built by :meth:`consolidate_topics`.  Issues one HTTP
        request per consolidated :class:`SourceQuery` (typically 1-2 requests
        total) to stay within the 3 req/sec rate limit.

        Parameters
        ----------
        since : datetime
            Lower bound (exclusive) for submission date.  Must be timezone-aware.
        topics : list[TopicRef]
            Topics to include; consolidated into 1-2 API queries.  An empty list
            triggers a single undirected date-range query.
        limit : int
            Maximum total results to return (across all consolidated queries).

        Returns
        -------
        list[PaperCreate]
            Deduplicated papers newer than *since*, ordered by submission date.
        """
        # Startup grace — lets containers finish their warm-up before first burst.
        # TODO: add startup_grace_seconds to user_config schema (follow-up).
        grace = getattr(getattr(self.config, "pulse", None), "startup_grace_seconds", 0.0)
        await _enforce_startup_grace(grace)

        # Persistent rate limiter (no-op when db_pool is None).
        p_limiter: PersistentSourceRateLimiter | None = None
        if self.db_pool is not None:
            p_limiter = PersistentSourceRateLimiter(
                source_type="arxiv",
                user_id=user_id,
                min_interval_seconds=RATE_LIMIT_DELAY,
                db_pool=self.db_pool,
                fallback=self._rate_limiter,
            )

        # Normalise *since* to UTC, then format as arXiv date string YYYYMMDDHHMM
        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        since_str = since_utc.strftime("%Y%m%d%H%M")
        # arXiv submittedDate ranges require minute precision on both bounds.
        # Use a far-future minute sentinel instead of the invalid legacy 99999999
        # or the underspecified date-only 29991231.
        date_filter = f"submittedDate:[{since_str} TO 299912312359]"

        if not topics:
            consolidated = [SourceQuery(topics=[], extra_params={})]
        else:
            consolidated = self.consolidate_topics(topics)

        seen_ids: set[str] = set()
        papers: list[PaperCreate] = []

        per_query = max(1, limit // max(len(consolidated), 1))

        for sq in consolidated:
            if len(papers) >= limit:
                break

            # Build search_query from SourceQuery.extra_params or from its topics.
            topic_q = sq.extra_params.get("search_query", "")
            if not topic_q and sq.topics:
                parts: list[str] = []
                for topic in sq.topics:
                    terms = topic.query_terms if topic.query_terms else [topic.name]
                    parts.extend(f'(ti:"{t}" OR abs:"{t}")' for t in terms)
                topic_q = " OR ".join(parts)

            if topic_q:
                search_query = f"({topic_q}) AND {date_filter}"
            else:
                search_query = date_filter

            params = {
                "search_query": search_query,
                "start": 0,
                "max_results": per_query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }

            started_at = _time.monotonic()
            candidate_count = 0

            # Acquire persistent rate limit slot before the in-process lock.
            if p_limiter is not None:
                await p_limiter.acquire()

            try:
                root = await self._fetch_xml(params)
            except Exception as _exc:
                logger.warning("arXiv fetch_new_since failed for query: %s", search_query)
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
                        await log_event(
                            pool=self.db_pool,
                            level="error",
                            category="source",
                            source="arxiv",
                            message="fetch_failed",
                            context={"http_status": None, "exception": repr(_exc)[:300]},
                        )
                    except Exception:
                        pass
                continue

            if root is None:
                logger.warning("arXiv fetch_new_since returned no data for query: %s", search_query)
                diag = self.last_poll_diagnostic or {}
                http_status = diag.get("status", "error")
                retry_after = diag.get("retry_after_s")
                p_status: str = "rate_limit" if http_status == "rate_limit" else "error"
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
                        _diag_code = diag.get("status_code")
                        if p_status == "rate_limit":
                            await log_event(
                                pool=self.db_pool,
                                level="warning",
                                category="source",
                                source="arxiv",
                                message="rate_limited",
                                context={
                                    "http_status": _diag_code or 429,
                                    "retry_after_s": retry_after,
                                },
                            )
                        else:
                            await log_event(
                                pool=self.db_pool,
                                level="error",
                                category="source",
                                source="arxiv",
                                message="fetch_failed",
                                context={
                                    "http_status": _diag_code,
                                    "exception": diag.get("message", "")[:300],
                                },
                            )
                    except Exception:
                        pass
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
                candidate_count += 1
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
                        source="arxiv",
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
                    "arxiv",
                    started_utc,
                    now_utc,
                    status,
                    candidate_count,
                    duration_ms,
                    "{}",
                )
        except Exception as exc:
            logger.warning("arXiv: failed to insert source_run_history: %s", exc)

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
