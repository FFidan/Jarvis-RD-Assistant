"""Abstract base class for paper source plugins.

All paper sources (arXiv, Semantic Scholar, etc.) must implement this interface.
Sources are discovered via the ``@register_source`` decorator in ``registry.py``.
"""

import asyncio
import logging
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    import asyncpg
    from pydantic import SecretStr

import httpx
from jarvis_common.event_log import log_event

# _MAX_RETRY_AFTER_S is re-exported (canonical value lives in jarvis_common.net);
# the redundant alias marks it as an intentional re-export so call sites can keep
# doing `from paper_ingestion.sources.base import _MAX_RETRY_AFTER_S`.
from jarvis_common.net import _MAX_RETRY_AFTER_S as _MAX_RETRY_AFTER_S
from jarvis_common.net import parse_retry_after as _parse_retry_after_value
from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter, SourceRateLimiter

from paper_ingestion.models import PaperCreate, PaperSourceConfig, TopicRef

logger = logging.getLogger(__name__)

# Module-level timestamp set at import time; used by _enforce_startup_grace.
_STARTUP_AT: float = _time.monotonic()


def parse_retry_after(exc: BaseException) -> int | None:
    """Extract a whole-second Retry-After delay from an exception's response.

    Delegates to :func:`jarvis_common.net.parse_retry_after`, which handles
    both RFC 7231 forms (delta-seconds and HTTP-date) and caps the result at
    :data:`_MAX_RETRY_AFTER_S`.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    return _parse_retry_after_value(response.headers.get("Retry-After"))


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
    _rate_limiter: SourceRateLimiter  # in-memory fallback; set by subclass __init__

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

    def _resolve_api_key(self, settings_key: "SecretStr | None") -> str | None:
        """Resolve the source API key, preferring the DB config over settings.

        The per-source ``config`` row may carry an ``api_key`` override; when it
        does not, fall back to the process-level ``settings_key`` secret (the
        env/config-file value). Returns ``None`` when neither is set.
        """
        cfg_key = self.config.config.get("api_key") if self.config.config else None
        return cfg_key or (settings_key.get_secret_value() if settings_key else None)

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
        """Whole-second Retry-After delay from a response, capped at the cap.

        Delegates to :func:`jarvis_common.net.parse_retry_after`, which handles
        both RFC 7231 forms (delta-seconds and HTTP-date) and caps at
        :data:`_MAX_RETRY_AFTER_S`.
        """
        if response is None:
            return None
        return _parse_retry_after_value(response.headers.get("Retry-After"))

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

    async def _record_fetch_outcome(
        self,
        *,
        started_at: float,
        candidate_count: int,
        user_id: int | None,
        status: str,
        p_limiter: "PersistentSourceRateLimiter | None",
        log_level: str,
        log_message: str,
        log_context: dict[str, Any],
        retry_after_s: int | None = None,
    ) -> None:
        """Record the terminal outcome of one ``fetch_new_since`` attempt.

        Consolidates the three side effects every ``fetch_new_since``
        implementation performs on each of its success / rate-limit / error
        paths, in this exact order:

        1. ``p_limiter.update_last_request(status[, retry_after_s])`` — advances
           the persistent rate-limiter slot (skipped when ``p_limiter`` is
           ``None``).  ``retry_after_s`` is forwarded only when provided.
        2. :meth:`_insert_run_history` — writes the ``source_run_history`` audit
           row (a no-op when ``db_pool`` is ``None``).  ``duration_ms`` is
           computed here from ``started_at`` so call sites no longer repeat it.
        3. :func:`jarvis_common.event_log.log_event` — emits the
           ``category='source'`` structured log row (only when ``db_pool`` is
           set).  ``log_level`` / ``log_message`` / ``log_context`` carry the
           per-path payload that differs across sources and outcomes.

        Parameters
        ----------
        started_at:
            ``time.monotonic()`` captured before the request; used to derive
            ``duration_ms``.
        candidate_count:
            Number of new papers accepted on this attempt (0 on rate-limit /
            error paths).
        user_id:
            Forwarded to the rate-limit slot and the audit row.
        status:
            Persistent-limiter / run-history status: ``"ok"`` | ``"rate_limit"``
            | ``"error"``.
        p_limiter:
            The per-(source, user) persistent limiter, or ``None`` when the
            in-process fallback is in use.
        log_level, log_message, log_context:
            Forwarded verbatim to ``log_event`` (``source`` is taken from
            ``self.source_type``).
        retry_after_s:
            Optional Retry-After hint forwarded to ``update_last_request`` on
            rate-limit paths.
        """
        # Capture duration before the limiter update so the audit row reflects
        # the fetch latency only (matches the pre-consolidation call-site timing).
        duration_ms = int((_time.monotonic() - started_at) * 1000)
        if p_limiter is not None:
            if retry_after_s is not None:
                await p_limiter.update_last_request(status, retry_after_s=retry_after_s)
            else:
                await p_limiter.update_last_request(status)

        await self._insert_run_history(
            started_at=started_at,
            status=status,
            candidate_count=candidate_count,
            duration_ms=duration_ms,
            user_id=user_id,
        )

        if self.db_pool is not None:
            try:
                await log_event(
                    pool=self.db_pool,
                    level=log_level,  # type: ignore[arg-type]
                    category="source",
                    source=self.source_type,
                    message=log_message,
                    context=log_context,
                )
            except Exception as exc:
                logger.warning(
                    "%s: log_event write failed for %s",
                    self.source_type,
                    log_message,
                    exc_info=exc,
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

    def make_persistent_rate_limiter(
        self,
        *,
        user_id: int | None,
        min_interval_seconds: float,
    ) -> "PersistentSourceRateLimiter | None":
        """Build the per-(source, user) persistent rate limiter for a poll run.

        Returns ``None`` when ``db_pool`` is absent (the limiter degrades to the
        in-process ``self._rate_limiter`` fallback at the call site).  Shared by
        every ``fetch_new_since`` implementation; only ``min_interval_seconds``
        differs per source.
        """
        if self.db_pool is None:
            return None
        return PersistentSourceRateLimiter(
            source_type=self.source_type,
            user_id=user_id,
            min_interval_seconds=min_interval_seconds,
            db_pool=self.db_pool,
            fallback=self._rate_limiter,
        )

    @staticmethod
    def _normalize_since_utc(since: datetime) -> datetime:
        """Return ``since`` as a UTC-aware datetime.

        Naive inputs are assumed to already be UTC; aware inputs are converted.
        """
        return since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)

    async def fetch_new_since(
        self,
        since: datetime,
        topics: list[TopicRef],
        limit: int = 100,
        user_id: int | None = None,
    ) -> list[PaperCreate]:
        """Fetch papers newer than ``since`` relevant to any of the given topics.

        ``user_id`` is threaded through to per-user rate-limit slots and
        ``source_run_history`` rows so multi-tenant pulse runs don't collide
        on shared limiter buckets.

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
