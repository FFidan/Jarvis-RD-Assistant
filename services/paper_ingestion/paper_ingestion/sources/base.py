"""Abstract base class for paper source plugins.

All paper sources (arXiv, Semantic Scholar, etc.) must implement this interface.
Sources are discovered via the ``@register_source`` decorator in ``registry.py``.
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, TypedDict

import httpx

from paper_ingestion.models import PaperCreate, PaperSourceConfig, TopicRef

logger = logging.getLogger(__name__)

# HTTP status codes that indicate transient server/rate-limit errors.
# Plugins return [] / None on these codes rather than raising.
_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


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

    Attributes
    ----------
    source_type : str
        Class-level identifier matching ``paper_sources.source_type``.
    """

    source_type: str  # must be set by subclass as class variable

    def __init__(self, config: PaperSourceConfig, http_client: httpx.AsyncClient) -> None:
        self.config = config
        self.http_client = http_client
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
        (2xx after ``raise_for_status``).  Returns ``None`` — rather than raising
        — for the following error classes:

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

    async def fetch_new_since(
        self,
        since: datetime,
        topics: list[TopicRef],
        limit: int = 100,
    ) -> list[PaperCreate]:
        """Fetch papers newer than ``since`` relevant to any of the given topics.

        Default: returns empty list. Sources that can poll by date override this.
        """
        return []

    async def get_recommendations(
        self,
        positive_seeds: list[str],
        negative_seeds: list[str] | None = None,
        limit: int = 50,
    ) -> list[PaperCreate]:
        """Recommend papers similar to positive seeds, dissimilar to negative seeds.

        ``positive_seeds``/``negative_seeds`` are source-native IDs (e.g. S2 paper IDs).
        Default: returns empty list. Sources with a recommendation endpoint override this.
        """
        return []
