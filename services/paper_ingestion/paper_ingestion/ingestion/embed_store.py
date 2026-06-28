"""Embedding generation + Qdrant collection/storage mixin.

Extracted verbatim from ``Embedder`` (C1 God-class decomposition).  Every
method body below is byte-for-byte identical to the original ``Embedder``
method; only the enclosing class changed (now a mixin composed into
``Embedder``).  ``self`` semantics are unchanged, so cross-calls such as
``self.embed_texts(...)`` resolve through the same MRO.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

import httpcore
import httpx
from jarvis_common.llm_client import build_litellm_headers, get_litellm_config
from qdrant_client.models import Distance, PointStruct, VectorParams

from paper_ingestion.ingestion.embedding_config import (
    _CHUNK_POINT_ID_NAMESPACE,
    COLLECTION_NAME,
    EMBED_REQUEST_TIMEOUT_SECONDS,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_NAME,
    _sanitize_embedding_error_detail,
    extract_qdrant_collection_dimension,
    raise_for_collection_dimension_mismatch,
    validate_embedding_configuration,
)
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.perf_probe import probe_span

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


class EmbeddingBatchError(RuntimeError):
    """A batch failed after one or more earlier batches were upserted.

    Carries the chunk/point-id pairs whose Qdrant upsert *did* succeed so the
    caller can persist their DB rows.  Those vectors are intentionally left in
    Qdrant: discarding them throws away minutes of CPU-bound embedding and
    makes a retry start from zero.  A retry resumes via the Phase-1 idempotency
    check + ``ON CONFLICT (paper_id, chunk_index) DO NOTHING``.
    """

    def __init__(
        self,
        message: str,
        *,
        completed_chunks: list[ChunkForEmbedding],
        completed_point_ids: list[str],
    ) -> None:
        super().__init__(message)
        self.completed_chunks = completed_chunks
        self.completed_point_ids = completed_point_ids


class EmbeddingStoreMixin:
    """Embedding generation, collection lifecycle, and Qdrant upsert/delete."""

    if TYPE_CHECKING:
        # Shared state provided by Embedder.__init__ — declared here so pyright
        # resolves attribute access inside this mixin without runtime overhead.
        qdrant: AsyncQdrantClient
        http_client: httpx.AsyncClient
        _collection_lock: asyncio.Lock
        _collection_ensured: bool

    async def ensure_collection(self) -> None:
        """Create the Qdrant collection if it does not exist.

        Uses ``EMBEDDING_DIMENSION`` from the environment. Idempotent:
        skips creation after first successful check.
        """
        if self._collection_ensured:
            return
        async with self._collection_lock:
            if self._collection_ensured:
                return
            validate_embedding_configuration()
            collections = await self.qdrant.get_collections()
            existing = {c.name for c in collections.collections}
            if COLLECTION_NAME not in existing:
                await self.qdrant.create_collection(
                    collection_name=COLLECTION_NAME,
                    vectors_config=VectorParams(
                        size=EMBEDDING_DIMENSION,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info(
                    "Created Qdrant collection '%s' (dim=%d)", COLLECTION_NAME, EMBEDDING_DIMENSION
                )
            else:
                collection_info = await self.qdrant.get_collection(collection_name=COLLECTION_NAME)
                current_dimension = extract_qdrant_collection_dimension(collection_info)
                raise_for_collection_dimension_mismatch(COLLECTION_NAME, current_dimension)
            self._collection_ensured = True

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for a batch of texts via LiteLLM.

        Parameters
        ----------
        texts : list[str]
            Texts to embed.

        Returns
        -------
        list[list[float]]
            Embedding vectors, one per input text.

        Raises
        ------
        RuntimeError
            If the embedding service times out, is unreachable, or returns
            an HTTP error after up to 3 attempts (5xx / read-timeout are
            retried with exponential backoff).

        Notes
        -----
        The per-request read timeout is ``EMBED_REQUEST_TIMEOUT_SECONDS``
        (default 300 s), not the historical 60 s scalar.  On memory-bound
        machines the embedding model is often GPU-evicted to CPU, where a
        32-chunk batch can take minutes; a 60 s ceiling turned that slow-path
        into a hard failure that discarded the whole paper.
        """
        if not texts:
            return []

        litellm_config = get_litellm_config()
        # Scalar timeouts collapse connect/read/write/pool into one budget; an
        # explicit Timeout keeps a tight connect while allowing a long read for
        # CPU-bound embedding.
        request_timeout = httpx.Timeout(
            EMBED_REQUEST_TIMEOUT_SECONDS,
            connect=10.0,
        )
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with probe_span("embed_texts_post", n_texts=len(texts), attempt=attempt):
                    response = await self.http_client.post(
                        f"{litellm_config.base_url}/v1/embeddings",
                        json={"model": EMBEDDING_MODEL, "input": texts},
                        headers=build_litellm_headers(litellm_config),
                        timeout=request_timeout,
                    )
                    response.raise_for_status()
                break
            except (httpx.TimeoutException, httpcore.ReadTimeout) as exc:
                # httpx.ReadTimeout subclasses httpx.TimeoutException; the bare
                # httpcore.ReadTimeout can still leak when httpx fails to wrap
                # it, so it is retried explicitly here.
                if attempt < 2:
                    last_exc = exc
                    logger.warning(
                        "Embedding service timed out (attempt %d/3): %r", attempt + 1, exc
                    )
                    await asyncio.sleep(2**attempt)
                    continue
                logger.error("Embedding service timed out: %r", exc, exc_info=True)
                raise RuntimeError("Embedding service timed out") from exc
            except httpx.ConnectError as exc:
                logger.error("Embedding service unavailable: %r", exc, exc_info=True)
                raise RuntimeError("Embedding service unavailable") from exc
            except httpx.HTTPStatusError as exc:
                is_retryable = exc.response.status_code >= 500
                if attempt < 2 and is_retryable:
                    last_exc = exc
                    logger.warning(
                        "Embedding service HTTP %d (attempt %d/3), retrying",
                        exc.response.status_code,
                        attempt + 1,
                    )
                    await asyncio.sleep(2**attempt)
                    continue
                body_preview = _sanitize_embedding_error_detail(exc.response.text or "")
                logger.error(
                    "Embedding service HTTP %d: %s", exc.response.status_code, body_preview
                )
                detail = f"Embedding service error (HTTP {exc.response.status_code})"
                if body_preview:
                    detail = f"{detail}: {body_preview}"
                raise RuntimeError(detail) from exc
        else:
            # All 3 retry attempts exhausted (timeout or 5xx path)
            msg = f"Embedding service failed after 3 attempts: {last_exc}"
            raise RuntimeError(msg) from last_exc

        data = response.json()

        # Sort by index to maintain order
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        embeddings = [item["embedding"] for item in sorted_data]

        # Validate ALL embedding dimensions match Qdrant collection config
        for idx, emb in enumerate(embeddings):
            if len(emb) != EMBEDDING_DIMENSION:
                raise ValueError(
                    f"Embedding dimension mismatch at index {idx}: got {len(emb)}, "
                    f"expected {EMBEDDING_DIMENSION}. Check EMBEDDING_DIMENSION env var "
                    f"and LiteLLM model config (litellm/config.yaml)."
                )

        return embeddings

    async def embed_and_store(
        self,
        paper_id: int,
        chunks: list[ChunkForEmbedding],
        batch_size: int = 32,
        *,
        user_id: int | None = None,
    ) -> list[str]:
        """Embed chunks and upsert into Qdrant.

        Parameters
        ----------
        paper_id : int
            The DB paper ID (stored as Qdrant payload for filtering).
        chunks : list[ChunkForEmbedding]
            Text chunks to embed and store.
        batch_size : int
            Number of chunks to embed per API call.
        user_id : int | None
            Owner of the source paper. NULL = canonical/shared (visible to all
            authenticated users via the OR-IS-NULL leg of the scope filter).

        Returns
        -------
        list[str]
            Qdrant point IDs (UUIDs), one per chunk, in chunk_index order.

        Raises
        ------
        EmbeddingBatchError
            When a batch fails after earlier batches were upserted.  The
            completed chunk/point pairs are attached so the caller can persist
            their DB rows; the completed Qdrant points are *kept* so a retry
            resumes instead of re-embedding from zero.
        """
        completed_chunks: list[ChunkForEmbedding] = []
        completed_point_ids: list[str] = []

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c.content for c in batch]
            try:
                embeddings = await self.embed_texts(texts)
                if len(embeddings) != len(texts):
                    raise RuntimeError(
                        f"Embedder returned {len(embeddings)} vectors for {len(texts)} texts;"
                        " refusing partial upsert"
                    )

                points = []
                batch_ids: list[str] = []
                for chunk, embedding in zip(batch, embeddings):
                    point_id = str(
                        uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, f"{paper_id}:{chunk.chunk_index}")
                    )
                    batch_ids.append(point_id)
                    points.append(
                        PointStruct(
                            id=point_id,
                            vector=embedding,
                            payload={
                                "paper_id": paper_id,
                                "chunk_index": chunk.chunk_index,
                                "page_number": chunk.page_number,
                                "content": chunk.content,
                                "embedding_model": EMBEDDING_MODEL_NAME,
                                "user_id": user_id,
                            },
                        )
                    )

                await self.qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
            except Exception as exc:
                if not completed_point_ids:
                    # Nothing persisted yet — wrap in EmbeddingBatchError so
                    # pdf_workflow.py:310 handles first-batch failures uniformly
                    # via the same resume logic as mid-batch failures.
                    raise EmbeddingBatchError(
                        f"Embedding failed at first batch (0 chunks persisted): {exc}",
                        completed_chunks=[],
                        completed_point_ids=[],
                    ) from exc
                logger.warning(
                    "Embedding batch %d failed for paper %d after %d/%d chunks persisted: %r",
                    i // batch_size,
                    paper_id,
                    len(completed_point_ids),
                    len(chunks),
                    exc,
                )
                raise EmbeddingBatchError(
                    f"Embedding failed at batch {i // batch_size} "
                    f"({len(completed_point_ids)}/{len(chunks)} chunks persisted): {exc}",
                    completed_chunks=completed_chunks,
                    completed_point_ids=completed_point_ids,
                ) from exc

            completed_chunks.extend(batch)
            completed_point_ids.extend(batch_ids)

        return completed_point_ids

    async def delete_paper_vectors(self, paper_id: int) -> None:
        """Delete all chunk vectors for a paper. Used by the hard-delete path.

        Failures propagate — callers are responsible for transaction coordination.

        Parameters
        ----------
        paper_id : int
            Database ID of the paper whose vectors should be removed from Qdrant.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        await self.qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="paper_id", match=MatchValue(value=paper_id))]
            ),
            wait=True,
        )
