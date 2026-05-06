"""Embedding service via LiteLLM's OpenAI-compatible API.

Handles: Markdown-aware text chunking, embedding generation, Qdrant storage,
collection initialization, and hybrid search (BM25 + semantic via RRF).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

import httpx
import tiktoken
from jarvis_common.llm_client import (
    build_litellm_headers,
    get_litellm_config,
)
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams

from paper_ingestion.models import ChunkForEmbedding

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "embed")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "qwen3-embedding:0.6b")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "1024"))
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")

COLLECTION_NAME = "paper_chunks"
CHUNK_TOKEN_LIMIT = 512
CHUNK_OVERLAP_TOKENS = 50

_KNOWN_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "nomic-embed-text": 768,
    "qwen3-embedding:0.6b": 1024,
}

_SENSITIVE_ERROR_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"(sk-[A-Za-z0-9._-]+)|"
    r"(Authorization:\s*)[^\s,;]+",
    re.IGNORECASE,
)


def _sanitize_embedding_error_detail(text: str, *, max_chars: int = 200) -> str:
    """Return a compact provider-error preview without secrets or noisy whitespace."""
    compact = " ".join(text.split())
    redacted = _SENSITIVE_ERROR_RE.sub(
        lambda match: f"{match.group(1) or match.group(3) or ''}<redacted>",
        compact,
    )
    return redacted[:max_chars]


def validate_embedding_configuration(
    *,
    model_name: str | None = None,
    dimension: int | None = None,
) -> None:
    """Fail clearly when a known fixed-dimension embedding model is misconfigured."""
    model_name = model_name or EMBEDDING_MODEL_NAME
    dimension = EMBEDDING_DIMENSION if dimension is None else dimension
    normalized_model_name = model_name.lower()
    for known_model, expected_dimension in _KNOWN_EMBEDDING_DIMENSIONS.items():
        if known_model in normalized_model_name and dimension != expected_dimension:
            raise RuntimeError(
                f"Embedding configuration mismatch: {model_name} outputs "
                f"{expected_dimension} dimensions, but EMBEDDING_DIMENSION={dimension}. "
                "Update EMBEDDING_DIMENSION or finish the Phase C Qdrant re-embed checkpoint."
            )


def extract_qdrant_collection_dimension(collection_info: object) -> int | None:
    """Return the single-vector collection size from Qdrant collection metadata."""
    config = getattr(collection_info, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    if vectors is None and isinstance(params, dict):
        vectors = params.get("vectors")
    if isinstance(vectors, dict):
        vector_config = vectors.get("") or next(iter(vectors.values()), None)
        if isinstance(vector_config, dict):
            return vector_config.get("size")
        return getattr(vector_config, "size", None)
    return getattr(vectors, "size", None)


def raise_for_collection_dimension_mismatch(
    collection_name: str,
    current_dimension: int | None,
    *,
    expected_dimension: int | None = None,
    model_name: str | None = None,
) -> None:
    """Raise when an existing Qdrant collection does not match the active embed config."""
    expected_dimension = EMBEDDING_DIMENSION if expected_dimension is None else expected_dimension
    model_name = model_name or EMBEDDING_MODEL_NAME
    if current_dimension == expected_dimension:
        return
    raise RuntimeError(
        f"Qdrant collection {collection_name!r} has dimension {current_dimension}; "
        f"expected {expected_dimension} for {model_name}. "
        "Run the documented Phase C Qdrant checkpoint/re-embed flow before restarting."
    )


def _point_payload(hit) -> dict | None:
    """Return a Qdrant point payload when present, else ``None``."""
    payload = getattr(hit, "payload", None)
    return payload if isinstance(payload, dict) else None


class Embedder:
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

    def chunk_text(
        self,
        text: str,
        page_boundaries: list[tuple[int, int]] | None = None,
    ) -> list[ChunkForEmbedding]:
        """Chunk Markdown text respecting structure and math blocks.

        Strategy:
        1. Split on section headings (## )
        2. Within sections, split on paragraph boundaries (double newline)
        3. Never split inside $$...$$ display math blocks
        4. If a unit exceeds CHUNK_TOKEN_LIMIT, sub-split at paragraph boundaries
        5. Accumulate small units until reaching target size

        Parameters
        ----------
        text : str
            Full extracted Markdown text from the PDF.
        page_boundaries : list[tuple[int, int]] | None
            List of ``(start_char, end_char)`` per page.  Index 0 corresponds
            to page 1 (1-indexed for user display).

        Returns
        -------
        list[ChunkForEmbedding]
            Chunks ready for embedding, with character offsets and page numbers.
        """
        enc = self._encoding

        def token_count(s: str) -> int:
            return len(enc.encode(s))

        def find_page(char_offset: int) -> int | None:
            if not page_boundaries:
                return None
            for page_idx, (start, end) in enumerate(page_boundaries):
                if start <= char_offset < end:
                    return page_idx + 1  # 1-indexed
            return len(page_boundaries)  # last page

        # Split into sections by headings, preserving the heading with each section
        sections = re.split(r"(?=\n##\s)", text)

        chunks: list[ChunkForEmbedding] = []
        chunk_index = 0
        current_text = ""
        current_start = 0
        text_offset = 0  # track position in original text

        for section in sections:
            if not section.strip():
                text_offset += len(section)
                continue

            section_tokens = token_count(section)

            if section_tokens <= CHUNK_TOKEN_LIMIT:
                # Section fits in one chunk -- try to accumulate with current
                combined = current_text + ("\n\n" if current_text else "") + section
                if token_count(combined) <= CHUNK_TOKEN_LIMIT:
                    if not current_text:
                        current_start = text_offset
                    current_text = combined
                else:
                    # Flush current chunk, start new
                    if current_text.strip():
                        mid = current_start + len(current_text) // 2
                        chunks.append(
                            ChunkForEmbedding(
                                chunk_index=chunk_index,
                                content=current_text.strip(),
                                page_number=find_page(mid),
                                start_char=current_start,
                                end_char=current_start + len(current_text),
                            )
                        )
                        chunk_index += 1
                    current_text = section
                    current_start = text_offset
            else:
                # Section too large -- flush current, then sub-split
                if current_text.strip():
                    mid = current_start + len(current_text) // 2
                    chunks.append(
                        ChunkForEmbedding(
                            chunk_index=chunk_index,
                            content=current_text.strip(),
                            page_number=find_page(mid),
                            start_char=current_start,
                            end_char=current_start + len(current_text),
                        )
                    )
                    chunk_index += 1
                    current_text = ""

                # Sub-split on paragraphs (double newline, but not inside $$...$$)
                paragraphs = re.split(r"\n\n(?!\$\$)", section)
                para_offset = text_offset

                for para in paragraphs:
                    if not para.strip():
                        para_offset += len(para) + 2  # +2 for \n\n
                        continue
                    combined = current_text + ("\n\n" if current_text else "") + para
                    if token_count(combined) <= CHUNK_TOKEN_LIMIT:
                        if not current_text:
                            current_start = para_offset
                        current_text = combined
                    else:
                        if current_text.strip():
                            mid = current_start + len(current_text) // 2
                            chunks.append(
                                ChunkForEmbedding(
                                    chunk_index=chunk_index,
                                    content=current_text.strip(),
                                    page_number=find_page(mid),
                                    start_char=current_start,
                                    end_char=current_start + len(current_text),
                                )
                            )
                            chunk_index += 1
                        # Force-split oversized paragraphs by token windows
                        if token_count(para) > CHUNK_TOKEN_LIMIT:
                            tokens = enc.encode(para)
                            # PI-CORE-005: track char advance via decoded window lengths
                            # instead of linear-interpolation which is inaccurate when
                            # token lengths vary (e.g. multibyte chars, BPE tokens).
                            char_advance = 0
                            for j in range(
                                0, len(tokens), CHUNK_TOKEN_LIMIT - CHUNK_OVERLAP_TOKENS
                            ):
                                window = tokens[j : j + CHUNK_TOKEN_LIMIT]
                                sub_text = enc.decode(window)
                                sub_start = para_offset + char_advance
                                mid = sub_start + len(sub_text) // 2
                                chunks.append(
                                    ChunkForEmbedding(
                                        chunk_index=chunk_index,
                                        content=sub_text.strip(),
                                        page_number=find_page(mid),
                                        start_char=sub_start,
                                        end_char=sub_start + len(sub_text),
                                    )
                                )
                                chunk_index += 1
                                # Advance only by the non-overlapping stride so the
                                # next window's start_char aligns with decoded text.
                                stride_end = j + CHUNK_TOKEN_LIMIT - CHUNK_OVERLAP_TOKENS
                                char_advance += len(enc.decode(tokens[j:stride_end]))
                            current_text = ""
                        else:
                            current_text = para
                            current_start = para_offset
                    para_offset += len(para) + 2

            text_offset += len(section)

        # Flush remaining
        if current_text.strip():
            mid = current_start + len(current_text) // 2
            chunks.append(
                ChunkForEmbedding(
                    chunk_index=chunk_index,
                    content=current_text.strip(),
                    page_number=find_page(mid),
                    start_char=current_start,
                    end_char=current_start + len(current_text),
                )
            )

        return chunks

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
            an HTTP error.
        """
        if not texts:
            return []

        litellm_config = get_litellm_config()
        try:
            response = await self.http_client.post(
                f"{litellm_config.base_url}/v1/embeddings",
                json={"model": EMBEDDING_MODEL, "input": texts},
                headers=build_litellm_headers(litellm_config),
                timeout=60.0,
            )
            response.raise_for_status()
        except httpx.TimeoutException as e:
            logger.error("Embedding service timed out: %r", e, exc_info=True)
            raise RuntimeError("Embedding service timed out") from e
        except httpx.ConnectError as e:
            logger.error("Embedding service unavailable: %r", e, exc_info=True)
            raise RuntimeError("Embedding service unavailable") from e
        except httpx.HTTPStatusError as e:
            body_preview = _sanitize_embedding_error_detail(e.response.text or "")
            logger.error("Embedding service HTTP %d: %s", e.response.status_code, body_preview)
            detail = f"Embedding service error (HTTP {e.response.status_code})"
            if body_preview:
                detail = f"{detail}: {body_preview}"
            raise RuntimeError(detail) from e
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

        Returns
        -------
        list[str]
            Qdrant point IDs (UUIDs), one per chunk, in chunk_index order.
        """
        all_point_ids: list[str] = []
        successfully_upserted: list[str] = []

        try:
            for i in range(0, len(chunks), batch_size):
                batch = chunks[i : i + batch_size]
                texts = [c.content for c in batch]
                embeddings = await self.embed_texts(texts)
                if len(embeddings) != len(texts):
                    raise RuntimeError(
                        f"Embedder returned {len(embeddings)} vectors for {len(texts)} texts;"
                        " refusing partial upsert"
                    )

                points = []
                batch_ids: list[str] = []
                for chunk, embedding in zip(batch, embeddings):
                    point_id = str(uuid.uuid4())
                    all_point_ids.append(point_id)
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
                            },
                        )
                    )

                await self.qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
                successfully_upserted.extend(batch_ids)
        except Exception:
            if successfully_upserted:
                try:
                    await self.qdrant.delete(
                        collection_name=COLLECTION_NAME,
                        points_selector=PointIdsList(points=successfully_upserted),  # type: ignore[arg-type]
                    )
                    logger.info(
                        "Cleaned up %d orphaned Qdrant points after batch failure",
                        len(successfully_upserted),
                    )
                except Exception:
                    logger.error(
                        "Failed to clean up %d orphaned Qdrant points",
                        len(successfully_upserted),
                    )
            raise

        return all_point_ids

    async def search_similar(
        self,
        query_text: str,
        limit: int = 10,
        paper_id_filter: int | None = None,
        score_threshold: float = 0.5,
    ) -> list[dict]:
        """Search Qdrant for chunks similar to query text.

        Parameters
        ----------
        query_text : str
            Text to find similar chunks for.
        limit : int
            Maximum number of results.
        paper_id_filter : int | None
            If set, exclude chunks from this paper.
        score_threshold : float
            Minimum cosine similarity score.

        Returns
        -------
        list[dict]
            Matching chunks with scores.
        """
        limit = max(1, min(limit, 100))
        score_threshold = max(0.0, min(score_threshold, 1.0))

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        query_embedding = (await self.embed_texts([query_text]))[0]

        query_filter = None
        if paper_id_filter is not None:
            query_filter = Filter(
                must_not=[
                    FieldCondition(
                        key="paper_id",
                        match=MatchValue(value=paper_id_filter),
                    )
                ]
            )

        response = await self.qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=query_embedding,
            limit=limit,
            query_filter=query_filter,
            score_threshold=score_threshold,
            with_payload=True,
        )

        results: list[dict] = []
        for hit in response.points:
            payload = _point_payload(hit)
            if payload is None:
                continue
            results.append(
                {
                    "paper_id": payload.get("paper_id"),
                    "chunk_index": payload.get("chunk_index"),
                    "content": (payload.get("content") or "")[:200],
                    "page_number": payload.get("page_number"),
                    "score": hit.score,
                }
            )
        return results

    async def search_chunks_in_paper(
        self,
        query_text: str,
        paper_id: int,
        limit: int = 5,
        score_threshold: float = 0.3,
    ) -> list[dict]:
        """Retrieve the top-k most relevant chunks from a specific paper.

        Used for conversational RAG: embeds the query and searches only
        within the given paper's Qdrant vectors.

        Parameters
        ----------
        query_text : str
            The user's question or query.
        paper_id : int
            Database ID of the paper to search within.
        limit : int
            Maximum chunks to return (1-20).
        score_threshold : float
            Minimum cosine similarity score (0.0-1.0).

        Returns
        -------
        list[dict]
            Each dict: {chunk_index, content, page_number, score}
        """
        limit = max(1, min(limit, 20))
        score_threshold = max(0.0, min(score_threshold, 1.0))

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        # Let RuntimeError from embed_texts propagate (callers handle it)
        query_embedding = (await self.embed_texts([query_text]))[0]

        try:
            response = await self.qdrant.query_points(
                collection_name=COLLECTION_NAME,
                query=query_embedding,
                limit=limit,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="paper_id",
                            match=MatchValue(value=paper_id),
                        )
                    ]
                ),
                score_threshold=score_threshold,
                with_payload=True,
            )
        except RuntimeError:
            raise
        except Exception:
            logger.exception("Qdrant search failed for paper %d", paper_id)
            return []

        results: list[dict] = []
        for hit in response.points:
            payload = _point_payload(hit)
            if payload is None:
                continue
            results.append(
                {
                    "chunk_index": payload.get("chunk_index"),
                    "content": payload.get("content") or "",
                    "page_number": payload.get("page_number"),
                    "score": hit.score,
                }
            )
        return results

    async def rerank_chunks(
        self,
        query: str,
        chunks: list[dict],
        top_k: int = 5,
    ) -> list[dict]:
        """Rerank retrieved chunks using a cross-encoder model.

        Falls back to returning the input chunks (truncated to top_k)
        if the reranker is unavailable.

        Parameters
        ----------
        query : str
            The search query.
        chunks : list[dict]
            Chunks from search_chunks_global or search_chunks_in_paper.
            Each must have a 'content' key.
        top_k : int
            Number of top results to return.

        Returns
        -------
        list[dict]
            Reranked chunks, limited to top_k.
        """
        from paper_ingestion.ingestion.reranker import get_reranker

        reranker = get_reranker()
        if reranker is None or len(chunks) <= top_k:
            return chunks[:top_k]

        passages = [c.get("content", "") for c in chunks]
        try:
            ranked = await asyncio.to_thread(reranker.rerank, query, passages, top_k)
            return [chunks[idx] for idx, _score in ranked]
        except Exception:
            logger.exception("Reranking failed; returning unranked results")
            return chunks[:top_k]

    async def compute_relevance(self, paper_text: str, topic_terms: list[str]) -> float:
        """Compute relevance score between a paper and topic terms.

        Embeds the paper title+abstract and each topic term, returns
        the maximum cosine similarity score.

        Parameters
        ----------
        paper_text : str
            Concatenated title and abstract of the paper.
        topic_terms : list[str]
            Topic query terms to compare against.

        Returns
        -------
        float
            Maximum cosine similarity between the paper and any topic term.
        """
        if not topic_terms:
            return 0.0
        try:
            all_texts = [paper_text] + topic_terms
            embeddings = await self.embed_texts(all_texts)
            paper_emb = embeddings[0]

            max_score = 0.0
            for term_emb in embeddings[1:]:
                dot = sum(a * b for a, b in zip(paper_emb, term_emb))
                norm_a = math.sqrt(sum(a * a for a in paper_emb))
                norm_b = math.sqrt(sum(b * b for b in term_emb))
                if norm_a > 0 and norm_b > 0:
                    score = dot / (norm_a * norm_b)
                    max_score = max(max_score, score)
            return round(max_score, 4)
        except Exception:
            logger.warning("Failed to compute relevance score", exc_info=True)
            return 0.0

    async def search_chunks_global(
        self,
        query_text: str,
        limit: int = 30,
        score_threshold: float = 0.2,
        user_id: int | None = None,
    ) -> list[dict]:
        """Search ALL chunks in Qdrant without a paper_id filter.

        Parameters
        ----------
        query_text : str
            Text to find similar chunks for.
        limit : int
            Maximum number of chunk results.
        score_threshold : float
            Minimum cosine similarity score.
        user_id : int | None
            Optional caller user ID for future multi-tenant chunk scoping.
            Currently a no-op — chunks do not yet have a ``user_id`` payload
            column.  When the multi-tenant schema pass adds that column, wire
            a Qdrant payload filter here.
            # TODO(multitenant): wire when chunks gain user_id

        Returns
        -------
        list[dict]
            Matching chunks with ``paper_id``, ``chunk_index``, ``content``,
            ``page_number``, and ``score``.
        """
        limit = max(1, min(limit, 200))
        score_threshold = max(0.0, min(score_threshold, 1.0))

        # Let RuntimeError from embed_texts propagate (callers handle it)
        query_embedding = (await self.embed_texts([query_text]))[0]

        try:
            response = await self.qdrant.query_points(
                collection_name=COLLECTION_NAME,
                query=query_embedding,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
            )
        except RuntimeError:
            raise
        except Exception:
            logger.exception("Qdrant global search failed")
            return []

        results: list[dict] = []
        for hit in response.points:
            payload = _point_payload(hit)
            if payload is None:
                continue
            results.append(
                {
                    "paper_id": payload.get("paper_id"),
                    "chunk_index": payload.get("chunk_index"),
                    "content": payload.get("content") or "",
                    "page_number": payload.get("page_number"),
                    "score": hit.score,
                }
            )
        return results

    async def hybrid_search(
        self,
        query: str,
        db_pool: asyncpg.Pool,
        limit: int = 10,
        offset: int = 0,
        k: int = 60,
    ) -> list[dict]:
        """Hybrid search combining BM25 keyword + semantic vector search via RRF.

        Uses Reciprocal Rank Fusion to combine rankings from PostgreSQL
        full-text search and Qdrant cosine similarity search.

        Parameters
        ----------
        query : str
            Natural language search query.
        db_pool : asyncpg.Pool
            Database connection pool for PostgreSQL BM25 search.
        limit : int
            Maximum number of results to return.
        offset : int
            Number of results to skip (for pagination).  The offset is applied
            *after* RRF fusion so that relative rankings are computed over the
            full candidate pool (``limit + offset`` items) before slicing.
            This preserves RRF correctness: both BM25 and semantic legs see
            the full candidate set, and the merged ranking is sliced at the
            end rather than before fusion.
        k : int
            RRF constant (higher = more weight to lower-ranked results).
            Standard value is 60.

        Returns
        -------
        list[dict]
            Ranked papers with: id, title, authors, url, abstract,
            published_date, rrf_score, bm25_rank, semantic_rank.
        """
        # ------------------------------------------------------------------
        # BM25 leg — PostgreSQL full-text search
        # ------------------------------------------------------------------
        # PI-CORE-006: fetch limit+offset candidates so pagination works correctly
        # after RRF fusion.  Cap at 200 to match search_chunks_global's guard.
        candidate_limit = min(limit + offset, 200)
        bm25_sql = """
            SELECT p.id, p.title, p.authors, p.url, p.abstract,
                   p.published_date,
                   ts_rank(p.search_vector,
                           websearch_to_tsquery('english', $1)) AS bm25_score
            FROM papers p
            WHERE p.search_vector @@ websearch_to_tsquery('english', $1)
            ORDER BY bm25_score DESC
            LIMIT $2
        """
        async with db_pool.acquire() as conn:
            bm25_rows = await conn.fetch(bm25_sql, query, candidate_limit)

        # Build rank map (1-indexed)
        bm25_rank_map: dict[int, int] = {}
        bm25_meta: dict[int, dict] = {}
        for rank, row in enumerate(bm25_rows, start=1):
            pid = row["id"]
            bm25_rank_map[pid] = rank
            bm25_meta[pid] = {
                "id": pid,
                "title": row["title"],
                "authors": row["authors"],
                "url": row["url"],
                "abstract": row["abstract"],
                "published_date": row["published_date"],
            }

        # ------------------------------------------------------------------
        # Semantic leg — Qdrant global chunk search, aggregated by paper
        # ------------------------------------------------------------------
        # PI-CORE-006: match the same candidate_limit so both legs see the full
        # pool needed to produce correct RRF rankings before offset is applied.
        chunks = await self.search_chunks_global(query, limit=candidate_limit, score_threshold=0.05)

        # Aggregate: max chunk score per paper
        paper_max_score: dict[int, float] = defaultdict(float)
        for chunk in chunks:
            pid = chunk["paper_id"]
            if pid is not None:
                paper_max_score[pid] = max(paper_max_score[pid], chunk["score"])

        # Sort papers by aggregated semantic score descending → rank map
        semantic_sorted = sorted(paper_max_score.items(), key=lambda x: x[1], reverse=True)
        semantic_rank_map: dict[int, int] = {
            pid: rank for rank, (pid, _) in enumerate(semantic_sorted, start=1)
        }

        # ------------------------------------------------------------------
        # Reciprocal Rank Fusion
        # ------------------------------------------------------------------
        all_paper_ids = set(bm25_rank_map) | set(semantic_rank_map)
        scored: list[tuple[int, float]] = []
        for pid in all_paper_ids:
            rrf_score = 0.0
            if pid in bm25_rank_map:
                rrf_score += 1.0 / (k + bm25_rank_map[pid])
            if pid in semantic_rank_map:
                rrf_score += 1.0 / (k + semantic_rank_map[pid])
            scored.append((pid, rrf_score))

        # Sort by RRF score descending, then by paper_id for stable ordering.
        # Slice to limit+offset candidates first, then apply offset pagination
        # after RRF so that the full merged ranking is preserved.
        scored.sort(key=lambda x: (-x[1], x[0]))
        top_ids = [pid for pid, _ in scored[offset : offset + limit]]

        # ------------------------------------------------------------------
        # Fetch metadata for papers found only in semantic leg
        # ------------------------------------------------------------------
        missing_ids = [pid for pid in top_ids if pid not in bm25_meta]
        if missing_ids:
            async with db_pool.acquire() as conn:
                meta_rows = await conn.fetch(
                    "SELECT id, title, authors, url, abstract, published_date "
                    "FROM papers WHERE id = ANY($1::int[])",
                    missing_ids,
                )
            for row in meta_rows:
                bm25_meta[row["id"]] = {
                    "id": row["id"],
                    "title": row["title"],
                    "authors": row["authors"],
                    "url": row["url"],
                    "abstract": row["abstract"],
                    "published_date": row["published_date"],
                }

        # ------------------------------------------------------------------
        # Build result list
        # ------------------------------------------------------------------
        rrf_map = dict(scored)
        results: list[dict] = []
        for pid in top_ids:
            meta = bm25_meta.get(pid)
            if meta is None:
                continue  # paper deleted between queries
            results.append(
                {
                    "id": meta["id"],
                    "title": meta["title"],
                    "authors": meta["authors"],
                    "url": meta["url"],
                    "abstract": meta["abstract"],
                    "published_date": meta["published_date"],
                    "rrf_score": round(rrf_map[pid], 8),
                    "bm25_rank": bm25_rank_map.get(pid),
                    "semantic_rank": semantic_rank_map.get(pid),
                }
            )

        return results

    async def delete_paper_vectors(self, paper_id: int) -> None:
        """Delete all chunk vectors for a paper. Used by hard-delete (Sprint 8 B1.1).

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

    async def discover_from_seeds(
        self,
        seed_paper_ids: list[int],
        db_pool: asyncpg.Pool,
        limit: int = 10,
        score_threshold: float = 0.5,
        max_points_per_seed: int = 10,
    ) -> list[dict]:
        """Discover papers similar to seed papers via Qdrant RecommendQuery.

        Uses AVERAGE_VECTOR strategy to compute a server-side centroid of
        all positive point vectors, then searches for similar chunks while
        excluding the seed papers themselves.

        Parameters
        ----------
        seed_paper_ids : list[int]
            Database IDs of the seed papers.
        db_pool : asyncpg.Pool
            Database connection pool for looking up paper metadata.
        limit : int
            Maximum number of discovered papers to return.
        score_threshold : float
            Minimum similarity score (0.0-1.0).
        max_points_per_seed : int
            Maximum Qdrant point IDs to sample per seed paper.

        Returns
        -------
        list[dict]
            Each dict: {paper_id, score, content} deduplicated by paper_id.
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchAny,
            MatchValue,
            RecommendInput,
            RecommendQuery,
            RecommendStrategy,
        )

        all_positive: list = []  # mix of point ID strings and raw vectors

        # Pass 1: Scroll Qdrant for each seed; collect IDs missing from Qdrant.
        missing_seed_ids: list[int] = []
        for seed_id in seed_paper_ids:
            # Scroll Qdrant for this seed's chunks
            seed_filter = Filter(
                must=[FieldCondition(key="paper_id", match=MatchValue(value=seed_id))]
            )
            records, _ = await self.qdrant.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=seed_filter,
                limit=1000,
                with_payload=False,
                with_vectors=False,
            )

            if records:
                # Sample evenly spaced point IDs
                ids = [str(r.id) for r in records]
                if len(ids) <= max_points_per_seed:
                    sampled = ids
                else:
                    step = len(ids) / max_points_per_seed
                    sampled = [ids[int(i * step)] for i in range(max_points_per_seed)]
                all_positive.extend(sampled)
            else:
                missing_seed_ids.append(seed_id)

        # Pass 2: Batch-fetch metadata for all missing seeds in a single DB round-trip,
        # then release the connection before doing any embedding network I/O.
        if missing_seed_ids:
            async with db_pool.acquire() as conn:
                missing_rows = await conn.fetch(
                    "SELECT id, title, abstract FROM papers WHERE id = ANY($1::bigint[])",
                    missing_seed_ids,
                )
            # Connection released here — embed outside the DB context.
            texts_to_embed: list[str] = []
            for row in missing_rows:
                title = row["title"] or ""
                abstract = row["abstract"] or ""
                if title or abstract:
                    texts_to_embed.append(f"{title}. {abstract}".strip())
            if texts_to_embed:
                vectors = await self.embed_texts(texts_to_embed)
                all_positive.extend(vectors)

        if not all_positive:
            return []

        # Query Qdrant with RecommendQuery
        # Request extra results so dedup still yields enough papers
        raw_limit = limit * 5

        response = await self.qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=RecommendQuery(
                recommend=RecommendInput(
                    positive=all_positive,
                    strategy=RecommendStrategy.AVERAGE_VECTOR,
                ),
            ),
            query_filter=Filter(
                must_not=[FieldCondition(key="paper_id", match=MatchAny(any=seed_paper_ids))]
            ),
            limit=raw_limit,
            score_threshold=score_threshold,
            with_payload=True,
        )

        # Deduplicate by paper_id, keep best score per paper
        best: dict[int, dict] = {}
        for hit in response.points:
            payload = _point_payload(hit)
            if payload is None:
                continue
            pid = payload.get("paper_id")
            if pid is None:
                continue
            score = hit.score
            if pid not in best or score > best[pid]["score"]:
                best[pid] = {
                    "paper_id": pid,
                    "score": score,
                    "content": (payload.get("content") or "")[:300],
                }

        # Sort by score descending and trim to requested limit
        results = sorted(best.values(), key=lambda x: x["score"], reverse=True)
        return results[:limit]


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
