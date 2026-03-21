"""Local PDF source plugin (stub).

Local PDFs are ingested via the ``POST /api/upload-pdf`` and
``POST /api/scan-local-pdfs`` endpoints, not through the standard
search/fetch interface.  This stub exists so that ``source_type="local"``
is present in the source registry.
"""

from app.models import PaperCreate, PaperSourceConfig
from app.sources.base import PaperSource
from app.sources.registry import register_source

import httpx


@register_source
class LocalSource(PaperSource):
    """Stub source for locally uploaded PDFs.

    Local PDFs bypass the search/fetch workflow entirely — they are
    imported via dedicated upload and directory-scan endpoints.  This
    class satisfies the registry contract so that ``source_type="local"``
    is recognized throughout the system.
    """

    source_type = "local"

    def __init__(self, config: PaperSourceConfig, http_client: httpx.AsyncClient) -> None:
        super().__init__(config, http_client)

    async def search(self, query: str, max_results: int = 10) -> list[PaperCreate]:
        """Return empty list — local PDFs do not support search.

        Use ``POST /api/upload-pdf`` or ``POST /api/scan-local-pdfs`` instead.
        """
        return []

    async def fetch_by_id(self, external_id: str) -> PaperCreate | None:
        """Return None — local PDFs are not fetched by external ID.

        Use ``POST /api/upload-pdf`` or ``POST /api/scan-local-pdfs`` instead.
        """
        return None
