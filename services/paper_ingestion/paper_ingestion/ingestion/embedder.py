"""Embedding service via LiteLLM's OpenAI-compatible API.

Handles: Markdown-aware text chunking, embedding generation, Qdrant storage,
collection initialization, and hybrid search (BM25 + semantic via RRF).

C1 decomposition note
---------------------
The former ~1240-line ``Embedder`` God class was split into cohesive sibling
modules.  This module is now a thin **composition façade**: it re-exports the
full public surface so existing imports
(``from paper_ingestion.ingestion.embedder import Embedder``, plus every
constant/helper that was previously importable here, and the back-compat shim
``paper_ingestion.embedder``) keep working unchanged.

Split:
  * ``embedding_config``  — constants + pure config/payload helpers
  * ``chunking``          — ``chunk_text`` logic (free function)
  * ``embed_store``       — embedding generation + Qdrant collection/storage
  * ``search``            — semantic/hybrid search, rerank, relevance, discovery

No behavior was changed: every method body was relocated byte-for-byte and
``Embedder`` composes the mixins via the same MRO, so ``self`` semantics and
cross-method calls (e.g. ``self.embed_texts``) are identical.

``import asyncio`` is retained at module scope because the test suite
monkeypatches ``paper_ingestion.ingestion.embedder.asyncio.sleep``.
"""

from __future__ import annotations

import asyncio  # noqa: F401  (retained: monkeypatch target embedder.asyncio.sleep)
import logging

import httpx
import tiktoken
from qdrant_client import AsyncQdrantClient

from paper_ingestion.ingestion.chunking import chunk_text as _chunk_text
from paper_ingestion.ingestion.embed_store import (  # noqa: F401
    EmbeddingBatchError,
    EmbeddingStoreMixin,
)

# Re-export the full public surface from the cohesion modules so external
# imports of any previously-public name remain valid.
from paper_ingestion.ingestion.embedding_config import (  # noqa: F401
    _CHUNK_POINT_ID_NAMESPACE,
    _KNOWN_EMBEDDING_DIMENSIONS,
    _SENSITIVE_ERROR_RE,
    CHUNK_OVERLAP_TOKENS,
    CHUNK_TOKEN_LIMIT,
    COLLECTION_NAME,
    EMBED_REQUEST_TIMEOUT_SECONDS,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_NAME,
    QDRANT_URL,
    _point_payload,
    _sanitize_embedding_error_detail,
    _user_scope_filter,
    extract_qdrant_collection_dimension,
    raise_for_collection_dimension_mismatch,
    validate_embedding_configuration,
)
from paper_ingestion.ingestion.search import EmbeddingSearchMixin
from paper_ingestion.models import ChunkForEmbedding

logger = logging.getLogger(__name__)


class Embedder(EmbeddingStoreMixin, EmbeddingSearchMixin):
    """Manages text embedding and Qdrant vector storage.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client for LiteLLM API calls.
    qdrant_client : AsyncQdrantClient
        Async Qdrant client instance.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        qdrant_client: AsyncQdrantClient,
    ) -> None:
        self.http_client = http_client
        self.qdrant = qdrant_client
        self._encoding = tiktoken.get_encoding("cl100k_base")
        self._collection_ensured = False
        self._collection_lock = asyncio.Lock()

    def chunk_text(
        self,
        text: str,
        page_anchors: list[tuple[int, int, int]] | None = None,
    ) -> list[ChunkForEmbedding]:
        """Chunk Markdown into per-page, embedding-sized chunks.

        Each page's markdown (``text[start:end]`` for its anchor) is chunked on
        its own, so a chunk never spans a page break and every chunk's
        ``page_number`` is exact.  ``chunk_index`` is then made unique across the
        whole document — Qdrant point IDs derive from ``paper_id:chunk_index``,
        so per-page restarts would collide and silently drop chunks.

        Parameters
        ----------
        text : str
            Full extracted Markdown (per-page segments joined by a blank line).
        page_anchors : list[tuple[int, int, int]] | None
            ``(start_char, end_char, page_no)`` anchors per rendered page, with
            ``page_no`` the 1-indexed physical PDF page from Docling provenance.
            ``None`` chunks ``text`` as a single unpaged unit.

        Returns
        -------
        list[ChunkForEmbedding]
            Chunks ready for embedding, with character offsets and page numbers.
        """
        if not page_anchors:
            return _chunk_text(text, None, self._encoding)
        chunks: list[ChunkForEmbedding] = []
        for start, end, page_no in page_anchors:
            page_md = text[start:end]
            chunks.extend(_chunk_text(page_md, [(0, len(page_md), page_no)], self._encoding))
        for index, chunk in enumerate(chunks):
            chunk.chunk_index = index
        return chunks


async def delete_paper_vectors(paper_id: int) -> None:
    """Delete all Qdrant chunk vectors for *paper_id*.

    Module-level entry point for job handlers and hard-delete paths that access
    the service via ``paper_ingestion._state.svc`` rather than FastAPI
    dependency injection.

    Failures propagate — the caller (B1.1 hard-delete handler) wraps SQL +
    Qdrant in a transaction and relies on propagation to trigger a rollback.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper whose vectors should be removed from Qdrant.
    """
    from paper_ingestion._state import svc  # noqa: PLC0415

    embedder = svc.embedder
    if embedder is None:
        raise RuntimeError("Embedder not initialised; cannot delete paper vectors")
    await embedder.delete_paper_vectors(paper_id)
