"""Semantic / hybrid search, rerank, relevance, and discovery mixin.

Extracted verbatim from ``Embedder`` (C1 God-class decomposition).  Every
method body below is byte-for-byte identical to the original ``Embedder``
method; only the enclosing class changed (now a mixin composed into
``Embedder``).  ``self`` semantics are unchanged, so cross-calls such as
``self.embed_texts(...)`` resolve through the same MRO.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from typing import TYPE_CHECKING

from paper_ingestion.ingestion.embedding_config import (
    COLLECTION_NAME,
    _point_payload,
    _user_scope_filter,
)
from paper_ingestion.perf_probe import probe_span

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


class EmbeddingSearchMixin:
    """Vector search, hybrid (BM25+RRF) search, reranking, and recommendations."""

    async def search_similar(
        self,
        query_text: str,
        limit: int = 10,
        paper_id_filter: int | None = None,
        score_threshold: float = 0.5,
        user_id: int | None = None,
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
        user_id : int | None
            If set, restrict to chunks owned by ``user_id`` or marked
            canonical (NULL payload).

        Returns
        -------
        list[dict]
            Matching chunks with scores.
        """
        limit = max(1, min(limit, 100))
        score_threshold = max(0.0, min(score_threshold, 1.0))

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        query_embedding = (await self.embed_texts([query_text]))[0]

        must_not_clauses: list = []
        if paper_id_filter is not None:
            must_not_clauses.append(
                FieldCondition(key="paper_id", match=MatchValue(value=paper_id_filter))
            )
        user_filter = _user_scope_filter(user_id)
        if must_not_clauses or user_filter:
            query_filter = Filter(
                must_not=must_not_clauses or None,
                should=user_filter.should if user_filter else None,
            )
        else:
            query_filter = None

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
        """Rerank retrieved chunks using the configured reranker backend.

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
        from jarvis_common.settings import get_reranker_settings

        if len(chunks) <= top_k:
            return chunks[:top_k]

        backend = get_reranker_settings().reranker_backend
        if backend == "qwen3":
            from paper_ingestion.ingestion.qwen3_reranker import get_qwen3_reranker

            reranker = get_qwen3_reranker()
        else:
            from paper_ingestion.ingestion.reranker import get_reranker

            reranker = get_reranker()

        if reranker is None:
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

        When ``user_id`` is set, results are scoped to chunks owned by that
        user OR marked canonical (``user_id`` payload IS NULL). When unset,
        no scope filter is applied (preserves single-tenant + procrastinate
        task code paths).

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
                query_filter=_user_scope_filter(user_id),
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
        user_id: int | None = None,
    ) -> list[dict]:
        """Hybrid search combining BM25 keyword + semantic vector search via RRF.

        ``user_id`` scopes only the semantic leg (passed through to
        ``search_chunks_global``). The BM25 leg is intentionally untouched —
        per-user paper visibility belongs at the router layer, not here.

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
            with probe_span("hybrid_search_bm25_sql", candidate_limit=candidate_limit):
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
        chunks = await self.search_chunks_global(
            query, limit=candidate_limit, score_threshold=0.05, user_id=user_id
        )

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
