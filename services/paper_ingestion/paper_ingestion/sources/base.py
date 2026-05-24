"""Abstract base class for paper source plugins.

All paper sources (arXiv, Semantic Scholar, etc.) must implement this interface.
Sources are discovered via the ``@register_source`` decorator in ``registry.py``.
"""

import asyncio
import logging
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import asyncpg

import httpx

from paper_ingestion.models import PaperCreate, PaperSourceConfig, TopicRef

logger = logging.getLogger(__name__)

# HTTP status codes that indicate transient server/rate-limit errors.
# Plugins return [] / None on these codes rather than raising.
_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Module-level timestamp set at import time; used by _enforce_startup_grace.
_STARTUP_AT: float = _time.monotonic()


async def _enforce_startup_grace(grace_seconds: float) -> None:
    """Sleep until at least ``grace_seconds`` have elapsed since process startup.

    Lets containers warm up before the first outbound HTTP burst.

    Parameters
    ----------
    grace_seconds:
        Minimum number of seconds that must elapse from process start before
        this coroutine returns.  A value <= 0 is a no-op.
    """
    if grace_seconds <= 0:
        return
    elapsed = _time.monotonic() - _STARTUP_AT
    remaining = grace_seconds - elapsed
    if remaining > 0:
        await asyncio.sleep(remaining)


@dataclass(frozen=True)
class SourceQuery:
    """Represents one API query to be issued to a source during Pulse discovery.

    Attributes
    ----------
    topics : list[TopicRef]
        Topics whose results should be merged from this query.
    extra_params : dict[str, Any]
        Source-specific query parameters (e.g. ``{"sort": "date"}``).
    """

    topics: list["TopicRef"] = field(default_factory=list)
    extra_params: dict[str, Any] = field(default_factory=dict)


class SourcePollDiagnostic(TypedDict, total=False):
    """Structured source polling diagnostic consumed by Pulse discovery."""

    status: str
    message: str
    status_code: int | None
    retry_after_s: int | None
    settings_hint: str | None


class PaperSource(ABC):
    """Abstract interface for a paper data source.

    Parameters
    ----------
    config : PaperSourceConfig
        Configuration from the paper_sources DB table.
    http_client : httpx.AsyncClient
        Shared async HTTP client (managed by FastAPI lifespan).
    db_pool : Any | None
        Optional asyncpg connection pool for rate-limiter persistence and
        ``source_run_history`` writes.

    Attributes
    ----------
    source_type : str
        Class-level identifier matching ``paper_sources.source_type``.
    """

    source_type: str  # must be set by subclass as class variable

    def __init__(
        self,
        config: PaperSourceConfig,
        http_client: httpx.AsyncClient,
        db_pool: "asyncpg.Pool | None" = None,
    ) -> None:
        self.config = config
        self.http_client = http_client
        self.db_pool: asyncpg.Pool | None = db_pool
        self._last_poll_diagnostic: SourcePollDiagnostic | None = None

    @property
    def last_poll_diagnostic(self) -> SourcePollDiagnostic | None:
        """Most recent source-level polling diagnostic, if the last poll degraded."""
        return self._last_poll_diagnostic

    def _clear_poll_diagnostic(self) -> None:
        self._last_poll_diagnostic = None

    def _set_poll_diagnostic(
        self,
        *,
        status: str,
        message: str,
        status_code: int | None = None,
        retry_after_s: int | None = None,
        settings_hint: str | None = None,
    ) -> None:
        self._last_poll_diagnostic = {
            "status": status,
            "message": message,
            "status_code": status_code,
            "retry_after_s": retry_after_s,
            "settings_hint": settings_hint,
        }

    @staticmethod
    def _retry_after_seconds(response: httpx.Response | None) -> int | None:
        if response is None:
            return None
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return None
        try:
            return int(float(retry_after))
        except (TypeError, ValueError):
            return None

    def _record_transient_poll_diagnostic(self, response: httpx.Response) -> None:
        status = "rate_limit" if response.status_code == 429 else "api_error"
        self._set_poll_diagnostic(
            status=status,
            message=(
                f"{self.__class__.__name__} rate limit reached. Retry later."
                if response.status_code == 429
                else f"{self.__class__.__name__} upstream returned HTTP {response.status_code}."
            ),
            status_code=response.status_code,
            retry_after_s=self._retry_after_seconds(response),
            settings_hint=None,
        )

    async def _safe_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response | None:
        """Rate-limit-safe GET that handles transient HTTP errors gracefully.

        Performs a GET request and returns the :class:`httpx.Response` on success
        (2xx after ``raise_for_status``).  Returns ``None`` -- rather than raising
        -- for the following error classes:

        * HTTP status in ``_TRANSIENT_STATUS_CODES`` (429, 500, 502, 503, 504):
          logged at WARNING level; indicates rate-limiting or upstream outage.
        * :class:`httpx.HTTPError` (connection errors, timeouts, etc.):
          logged at WARNING level.

        Any other non-2xx status still raises :class:`httpx.HTTPStatusError`
        so callers see unexpected errors (e.g. 403 Forbidden) rather than
        silently getting ``None``.

        Parameters
        ----------
        url : str
            Full URL to request.
        params : dict | None
            Query parameters forwarded to ``httpx.AsyncClient.get``.
        headers : dict | None
            Extra request headers.
        timeout : float
            Request timeout in seconds (default 30 s).

        Returns
        -------
        httpx.Response | None
            Parsed response, or ``None`` on transient / network error.
        """
        try:
            response = await self.http_client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            if response.status_code in _TRANSIENT_STATUS_CODES:
                logger.warning(
                    "%s _safe_get %s returned %d; returning None",
                    self.source_type,
                    url,
                    response.status_code,
                )
                self._record_transient_poll_diagnostic(response)
                return None
            response.raise_for_status()
            self._clear_poll_diagnostic()
            return response
        except httpx.HTTPError as exc:
            logger.warning("%s _safe_get %s failed: %s", self.source_type, url, exc)
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
            return None

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        sort_by: str = "relevance",
        author: str | None = None,
    ) -> list[PaperCreate]:
        """Search for papers matching the query.

        Parameters
        ----------
        query : str
            Free-text search query.
        max_results : int
            Maximum number of results to return.
        year_from : int | None
            Filter results to papers published from this year (inclusive).
        year_to : int | None
            Filter results to papers published up to this year (inclusive).
        sort_by : str
            Sort order: ``"relevance"`` (default) or ``"date"``.
        author : str | None
            Filter results by author name.

        Returns
        -------
        list[PaperCreate]
            Papers with metadata populated entirely from the source API.
            No fields may be LLM-generated.
        """
        ...

    @abstractmethod
    async def fetch_by_id(self, external_id: str) -> PaperCreate | None:
        """Fetch a single paper by its source-specific external ID.

        Parameters
        ----------
        external_id : str
            The source-specific identifier (e.g., arXiv ID ``"2301.12345"``).

        Returns
        -------
        PaperCreate | None
            The paper if found, None otherwise.
        """
        ...

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
                    self.source_type,
                    started_utc,
                    now_utc,
                    status,
                    candidate_count,
                    duration_ms,
                    "{}",
                )
        except Exception as exc:
            logger.warning(
                "%s: failed to insert source_run_history: %s",
                self.source_type,
                exc,
                exc_info=True,
            )

    async def apply_startup_grace(self) -> None:
        """Sleep until the configured startup grace period has elapsed.

        Reads ``self.config.pulse.startup_grace_seconds`` (defaulting to 0.0
        when the attribute chain is absent) and delegates to
        :func:`_enforce_startup_grace`.  Call once at the top of every
        ``fetch_new_since`` implementation instead of repeating the
        three-line getattr chain inline.
        """
        grace = getattr(getattr(self.config, "pulse", None), "startup_grace_seconds", 0.0)
        await _enforce_startup_grace(grace)

    async def fetch_new_since(
        self,
        since: datetime,
        topics: list[TopicRef],
        limit: int = 100,
        user_id: int | None = None,
    ) -> list[PaperCreate]:
        """Fetch papers newer than ``since`` relevant to any of the given topics.

        ``user_id`` (Phase 2 WS-2D) is threaded through to per-user rate-limit
        slots and ``source_run_history`` rows so multi-tenant pulse runs don't
        collide on shared limiter buckets.

        Default: returns empty list. Sources that can poll by date override this.
        """
        return []

    def consolidate_topics(self, topics: list["TopicRef"]) -> list["SourceQuery"]:
        """Group topics into 1+ API queries. Default: one query per topic.

        Subclasses may override to batch multiple topics into a single API
        call when the upstream API supports it (e.g. comma-separated terms).

        Requirements for overrides:
        - Must be deterministic: same ``topics`` input -> same queries output.
        - Should respect a ~1500-character URL ceiling per query.

        Parameters
        ----------
        topics : list[TopicRef]
            Topics to be covered by this polling run.

        Returns
        -------
        list[SourceQuery]
            One or more queries. Default implementation returns one
            :class:`SourceQuery` per topic.
        """
        return [SourceQuery(topics=[t]) for t in topics]
