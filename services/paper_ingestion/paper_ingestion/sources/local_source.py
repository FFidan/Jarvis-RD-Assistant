"""Local PDF source plugin (stub).

Local PDFs are ingested via the ``POST /api/upload-pdf`` and
``POST /api/scan-local-pdfs`` endpoints, not through the standard
search/fetch interface.  This stub exists so that ``source_type="local"``
is present in the source registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import asyncpg

from paper_ingestion.models import PaperCreate, PaperSourceConfig
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.registry import register_source


@register_source
class LocalSource(PaperSource):
    """Stub source for locally uploaded PDFs.

    Local PDFs bypass the search/fetch workflow entirely — they are
    imported via dedicated upload and directory-scan endpoints.  This
    class satisfies the registry contract so that ``source_type="local"``
    is recognized throughout the system.
    """

    source_type = "local"
    supports_pulse_polling = False

    def __init__(
        self,
        config: PaperSourceConfig,
        http_client: httpx.AsyncClient,
        db_pool: asyncpg.Pool | None = None,
    ) -> None:
        super().__init__(config, http_client, db_pool)

    async def search(
        self,
        query: str,
        max_results: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        sort_by: str = "relevance",
        author: str | None = None,
    ) -> list[PaperCreate]:
        """Return empty list — local PDFs do not support search.

        Use ``POST /api/upload-pdf`` or ``POST /api/scan-local-pdfs`` instead.
        The extra kwargs (``year_from``, ``year_to``, ``sort_by``, ``author``) are
        accepted for signature compatibility with the ``PaperSource`` registry contract
        but are intentionally unused.
        """
        return []

    async def fetch_by_id(self, external_id: str) -> PaperCreate | None:
        """Return None — local PDFs are not fetched by external ID.

        Use ``POST /api/upload-pdf`` or ``POST /api/scan-local-pdfs`` instead.
        """
        return None
