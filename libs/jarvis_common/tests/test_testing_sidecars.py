"""Boundary-adapter tests for canonical local sidecars."""

from __future__ import annotations

import httpx
import pytest
from jarvis_common.llm_client import (
    ChatCompletionOptions,
    LiteLLMConfig,
    embed_texts,
    request_chat_completion_content,
)
from jarvis_common.testing_sidecars import FauxOllamaServer, FauxQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

pytestmark = pytest.mark.asyncio


async def test_faux_ollama_serves_litellm_embedding_and_chat_over_real_http():
    """FauxOllamaServer exercises real HTTP for LiteLLM-compatible calls."""
    async with FauxOllamaServer(
        dimension=4,
        chat_responses={"summarize": "deterministic summary"},
    ) as sidecar:
        async with httpx.AsyncClient() as client:
            vectors = await embed_texts(
                client,
                ["alpha", "beta"],
                config=LiteLLMConfig(base_url=sidecar.url),
            )
            content = await request_chat_completion_content(
                client,
                prompt="summarize",
                options=ChatCompletionOptions(model="smart"),
                config=LiteLLMConfig(base_url=sidecar.url),
            )

    assert len(vectors) == 2
    assert all(len(vector) == 4 for vector in vectors)
    assert vectors[0] != vectors[1]
    assert content == "deterministic summary"


async def test_faux_qdrant_filters_scores_counts_and_deletes_points():
    """FauxQdrantClient preserves Qdrant payload filtering semantics."""
    qdrant = FauxQdrantClient()
    await qdrant.create_collection(
        collection_name="paper_chunks",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    await qdrant.upsert(
        collection_name="paper_chunks",
        points=[
            PointStruct(id="a", vector=[1.0, 0.0], payload={"paper_id": 1, "user_id": 7}),
            PointStruct(id="b", vector=[0.0, 1.0], payload={"paper_id": 2, "user_id": 8}),
            PointStruct(id="c", vector=[0.9, 0.1], payload={"paper_id": 3, "user_id": 7}),
        ],
    )

    user_filter = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=7))])
    response = await qdrant.query_points(
        collection_name="paper_chunks",
        query=[1.0, 0.0],
        query_filter=user_filter,
        limit=10,
        with_payload=True,
    )
    count = await qdrant.count(collection_name="paper_chunks", count_filter=user_filter)

    assert [point.id for point in response.points] == ["a", "c"]
    assert count.count == 2

    await qdrant.delete(collection_name="paper_chunks", points_selector=Filter(must=[]))
    remaining = await qdrant.count(collection_name="paper_chunks")
    assert remaining.count == 0
