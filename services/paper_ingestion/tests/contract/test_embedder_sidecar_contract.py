"""Embedder boundary contracts backed by faux LiteLLM and faux Qdrant sidecars.

Survivor-of: mock-heavy embedder/Qdrant tests that asserted calls against
``AsyncMock`` clients instead of exercising the real Embedder boundary flow.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_embedder_sidecars_store_and_search_user_scoped_vectors(monkeypatch):
    """Embedder uses real HTTP embeddings plus Qdrant-compatible search/storage.

    # Verified: services/paper_ingestion/paper_ingestion/ingestion/embed_store.py:107
    # Verified: services/paper_ingestion/paper_ingestion/ingestion/search.py:130
    # Verified: services/paper_ingestion/paper_ingestion/ingestion/search.py:288
    """
    from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    async with FauxOllamaServer(dimension=EMBEDDING_DIMENSION) as llm:
        monkeypatch.setenv("LITELLM_BASE_URL", llm.url)
        async with httpx.AsyncClient() as http_client:
            qdrant = FauxQdrantClient()
            embedder = Embedder(http_client, qdrant)
            await embedder.ensure_collection()

            await embedder.embed_and_store(
                10,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="alpha methods and reproducibility",
                        page_number=1,
                        start_char=0,
                        end_char=33,
                    ),
                    ChunkForEmbedding(
                        chunk_index=1,
                        content="beta results and limitations",
                        page_number=2,
                        start_char=34,
                        end_char=62,
                    ),
                ],
                user_id=7,
            )
            await embedder.embed_and_store(
                11,
                [
                    ChunkForEmbedding(
                        chunk_index=0,
                        content="private paper for a different user",
                        page_number=1,
                        start_char=0,
                        end_char=34,
                    )
                ],
                user_id=8,
            )

            in_paper = await embedder.search_chunks_in_paper(
                "alpha reproducibility",
                paper_id=10,
                limit=5,
                score_threshold=0.0,
            )
            scoped_global = await embedder.search_chunks_global(
                "paper",
                user_id=7,
                limit=10,
                score_threshold=0.0,
            )

    assert {row["chunk_index"] for row in in_paper} == {0, 1}
    assert {row["paper_id"] for row in scoped_global} == {10}
