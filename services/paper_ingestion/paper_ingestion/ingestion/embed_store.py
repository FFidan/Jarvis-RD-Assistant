"""Embedding generation and Qdrant collection/storage behavior for ``Embedder``."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpcore
import httpx
from jarvis_common.llm_client import build_litellm_headers, get_litellm_config
from jarvis_common.maintenance import ensure_outbound_egress_allowed
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
from paper_ingestion.ingestion.payload_schema import VectorVisibility
from paper_ingestion.models import ChunkForEmbedding
from paper_ingestion.perf_probe import probe_span

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)

EmbeddingProgressCallback = Callable[[int, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class EmbeddingRunContext:
    """Optional resume and progress state for one embedding operation."""

    resume_content: dict[int, str] = field(default_factory=dict)
    progress_callback: EmbeddingProgressCallback | None = None


class EmbeddingBatchError(RuntimeError):
    """A batch failed after one or more earlier batches were upserted.

    Carries the chunk/point-id pairs whose Qdrant upsert *did* succeed so the
    caller can persist their DB rows.  Those vectors are intentionally left in
    Qdrant: discarding them throws away minutes of CPU-bound embedding and
    makes a retry start from zero.  A retry resumes by skipping any chunk
    whose content is unchanged and was embedded by the current model (see
    ``embed_and_store``'s ``resume_content``); a chunk with changed content, a
    stale embedding model, or a missing Qdrant point is re-embedded.
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


def chunk_point_id(paper_id: int, chunk_index: int) -> str:
    """Deterministic uuid5 Qdrant point ID for a (paper_id, chunk_index) pair."""
    return str(uuid.uuid5(_CHUNK_POINT_ID_NAMESPACE, f"{paper_id}:{chunk_index}"))


def chunk_embedding_fingerprint(content: str, *, model_name: str | None = None) -> str:
    """Return the durable identity of a model vector for chunk content."""
    active_model = EMBEDDING_MODEL_NAME if model_name is None else model_name
    return hashlib.sha256(f"{active_model}\0{content}".encode()).hexdigest()


class EmbeddingStoreMixin:
    """Embedding generation, collection lifecycle, and Qdrant upsert/delete."""

    if TYPE_CHECKING:
        import asyncpg

        # Shared state provided by Embedder.__init__ — declared here so pyright
        # resolves attribute access inside this mixin without runtime overhead.
        qdrant: AsyncQdrantClient
        http_client: httpx.AsyncClient
        _collection_lock: asyncio.Lock
        _collection_ensured: bool
        _db_pool: asyncpg.Pool | None

    async def ensure_collection(self) -> None:
        """Create and validate the Qdrant collection and visibility schema.

        Returns
        -------
        None
            The collection and all required authorization indexes are ready.

        Raises
        ------
        RuntimeError
            If the configured embedding dimension conflicts with the active
            model or an existing collection.
        qdrant_client.http.exceptions.UnexpectedResponse
            If Qdrant rejects collection or payload-index setup.
        asyncpg.PostgresError
            If the visibility checkpoint cannot be initialized or validated.

        Notes
        -----
        Uses ``EMBEDDING_DIMENSION`` from the environment, creates the payload
        indexes required by authorization filters, and initializes or rotates
        the deployment checkpoint when a database pool is available. The
        method is idempotent after every required step succeeds. Creating a
        collection with a database pool rotates the visibility generation so
        authenticated search under-fetches until reconciliation completes.
        """
        if self._collection_ensured:
            return
        async with self._collection_lock:
            if self._collection_ensured:
                return
            validate_embedding_configuration()
            collections = await self.qdrant.get_collections()
            existing = {c.name for c in collections.collections}
            collection_created = COLLECTION_NAME not in existing
            if collection_created:
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
            from paper_ingestion.ingestion.payload_schema import (  # noqa: PLC0415
                ensure_visibility_payload_indexes,
                prepare_visibility_schema,
            )

            if self._db_pool is None:
                await ensure_visibility_payload_indexes(self.qdrant)
            else:
                async with self._db_pool.acquire() as conn:
                    await prepare_visibility_schema(
                        conn,
                        self.qdrant,
                        collection_created=collection_created,
                    )
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
        OutboundEgressBlockedError
            If restored credentials await review when a non-empty batch is
            about to send.
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
                    ensure_outbound_egress_allowed("paper embedding request")
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

    async def embed_and_store(  # noqa: PLR0913 - explicit storage boundary inputs
        self,
        paper_id: int,
        chunks: list[ChunkForEmbedding],
        batch_size: int = 32,
        *,
        user_id: int | None = None,
        visibility: VectorVisibility | None = None,
        run_context: EmbeddingRunContext | None = None,
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
            Legacy compatibility/audit owner. This payload never grants access.
        visibility : VectorVisibility | None
            Persisted source, scope, and current deployment generation. A
            missing value writes complete fail-closed compatibility metadata;
            production ingestion always supplies the current value explicitly.
        run_context : EmbeddingRunContext | None
            Optional prior chunk content and progress callback for this run.
            Unchanged prior chunks are skipped, and progress advances only
            after a successful upsert or fully resume-skipped batch.

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
        run_context = run_context or EmbeddingRunContext()
        visibility = visibility or VectorVisibility.fail_closed()
        resume_content = run_context.resume_content
        progress_callback = run_context.progress_callback
        completed_chunks: list[ChunkForEmbedding] = []
        completed_point_ids: list[str] = []
        batch_offsets = range(0, len(chunks), batch_size)
        total_batches = len(batch_offsets)

        for batch_number, i in enumerate(batch_offsets, start=1):
            batch = chunks[i : i + batch_size]
            to_embed = [c for c in batch if resume_content.get(c.chunk_index) != c.content]
            try:
                if to_embed:
                    texts = [c.content for c in to_embed]
                    embeddings = await self.embed_texts(texts)
                    if len(embeddings) != len(texts):
                        raise RuntimeError(
                            f"Embedder returned {len(embeddings)} vectors for {len(texts)} texts;"
                            " refusing partial upsert"
                        )

                    points = [
                        PointStruct(
                            id=chunk_point_id(paper_id, chunk.chunk_index),
                            vector=embedding,
                            payload={
                                "paper_id": paper_id,
                                "chunk_index": chunk.chunk_index,
                                "page_number": chunk.page_number,
                                "content": chunk.content,
                                "embedding_model": EMBEDDING_MODEL_NAME,
                                "embedding_fingerprint": chunk_embedding_fingerprint(chunk.content),
                                "user_id": user_id,
                                **visibility.payload,
                            },
                        )
                        for chunk, embedding in zip(to_embed, embeddings)
                    ]
                    await self.qdrant.upsert(
                        collection_name=COLLECTION_NAME,
                        points=points,
                        wait=True,
                    )
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
            completed_point_ids.extend(chunk_point_id(paper_id, c.chunk_index) for c in batch)
            if progress_callback is not None:
                try:
                    await progress_callback(batch_number, total_batches)
                except Exception as exc:
                    raise EmbeddingBatchError(
                        "Embedding progress reporting failed after "
                        f"{len(completed_point_ids)}/{len(chunks)} chunks persisted: {exc}",
                        completed_chunks=completed_chunks,
                        completed_point_ids=completed_point_ids,
                    ) from exc

        return completed_point_ids

    async def delete_paper_vectors(self, paper_id: int) -> None:
        """Delete all chunk vectors for a paper. Used by the hard-delete path.

        Failures propagate — callers are responsible for transaction coordination.

        Parameters
        ----------
        paper_id : int
            Database ID of the paper whose vectors should be removed from Qdrant.

        Returns
        -------
        None
            Qdrant confirmed the filtered deletion.

        Raises
        ------
        qdrant_client.http.exceptions.UnexpectedResponse
            If Qdrant rejects the deletion.
        httpx.HTTPError
            If the Qdrant transport fails.

        Notes
        -----
        The call waits for Qdrant to apply the deletion. It does not alter the
        relational paper row; hard-delete callers coordinate that transaction.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        await self.qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=Filter(
                must=[FieldCondition(key="paper_id", match=MatchValue(value=paper_id))]
            ),
            wait=True,
        )
