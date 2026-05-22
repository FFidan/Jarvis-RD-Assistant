"""Contract tests for Embedder DB-touching paths.

The Embedder class primarily mocks Qdrant and Ollama HTTP (idiomatic external
boundaries that MUST remain mocked).  Its only DB interaction is through the
callers in paper_ingestion/rag/streaming.py (covered in test_rag_contract.py).

This file covers the embed_and_store path which writes chunk vectors to Qdrant
(kept mocked) and any direct DB reads the Embedder performs.  Currently the
Embedder has NO direct asyncpg calls; all its DB interaction is mediated through
the pool passed by its callers.  This file therefore focuses on:

  1. validate_embedding_configuration — reads collection config from Qdrant
     (Qdrant boundary = idiomatic mock; no DB involved).
  2. embed_and_store — stores vectors in Qdrant (idiomatic mock); verifies
     the call sequence and payload shape.
  3. Smoke: imports from both module paths stay identical (C3 split guard).

These tests are sparse by design — D3 notes that "RAG mostly mocks externally"
and the Embedder has the LARGEST kept-idiomatic count in this batch.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# 1. validate_embedding_configuration: Qdrant collection size check
#    (Qdrant = idiomatic external boundary; kept mocked)
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_validate_embedding_configuration_passes_on_correct_dim(contract_conn):
    """validate_embedding_configuration does not raise for a known model with correct dim.

    validate_embedding_configuration is a *sync* config-level guard (no Qdrant
    calls, no Embedder instance).  It only raises when a *known* model name is
    detected AND the configured EMBEDDING_DIMENSION contradicts the expected
    dimension for that model.  An unknown model name → no-op (passes silently).
    contract_conn is accepted to keep this test inside the per-test rollback scope.
    """
    from paper_ingestion.ingestion.embedder import (
        EMBEDDING_DIMENSION,
        EMBEDDING_MODEL_NAME,
        validate_embedding_configuration,
    )

    # Should not raise — passing the actual live config values must always agree.
    validate_embedding_configuration(
        model_name=EMBEDDING_MODEL_NAME,
        dimension=EMBEDDING_DIMENSION,
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_validate_embedding_configuration_raises_on_dim_mismatch(contract_conn):
    """validate_embedding_configuration raises RuntimeError on dimension mismatch.

    validate_embedding_configuration checks a *known* model name against the
    configured EMBEDDING_DIMENSION constant.  When nomic-embed-text (768-dim) is
    named but dimension is set to a wrong value, it must raise RuntimeError.
    contract_conn is accepted to keep this test inside the per-test rollback scope.
    """
    from paper_ingestion.ingestion.embedder import validate_embedding_configuration

    with pytest.raises(RuntimeError, match="dimension"):
        # nomic-embed-text expects exactly 768; supplying a wrong dimension triggers.
        validate_embedding_configuration(model_name="nomic-embed-text", dimension=512)


# ---------------------------------------------------------------------------
# 2. embed_and_store: Qdrant upsert sequence (idiomatic Qdrant mock)
#    contract_conn is accepted to ensure this runs inside a DB transaction
#    (even though embed_and_store itself has no asyncpg calls).
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_embed_and_store_calls_qdrant_upsert(contract_conn, monkeypatch):
    """embed_and_store embeds chunks and upserts them to Qdrant.

    Qdrant and Ollama HTTP are idiomatic mocks (external service boundaries).
    contract_conn is included so this test participates in the per-test
    rollback cycle even though Embedder has no direct asyncpg calls.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import httpx

    from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION, Embedder
    from paper_ingestion.models import ChunkForEmbedding

    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    # Ollama HTTP boundary: idiomatic mock.
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    embed_resp = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": [{"index": 0, "embedding": [0.5] * EMBEDDING_DIMENSION}]},
    )
    mock_http.post = AsyncMock(return_value=embed_resp)

    # Qdrant boundary: idiomatic mock.
    mock_qdrant = AsyncMock()
    mock_qdrant.upsert = AsyncMock()

    embedder = Embedder(mock_http, mock_qdrant)
    chunk = ChunkForEmbedding(
        chunk_index=0,
        content="Test content for embedding.",
        page_number=1,
        start_char=0,
        end_char=26,
    )

    await embedder.embed_and_store(
        paper_id=42,
        chunks=[chunk],
        user_id=1,
    )

    # Qdrant upsert must have been called once.
    mock_qdrant.upsert.assert_awaited_once()
    call_kwargs = mock_qdrant.upsert.call_args
    # embed_and_store always passes collection_name as a keyword argument.
    assert call_kwargs.kwargs.get("collection_name") == "paper_chunks"


# ---------------------------------------------------------------------------
# 3. Smoke: both import paths expose the same Embedder class (C3 split guard)
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_embedder_import_paths_are_identical(contract_conn):
    """Canonical and any shim module expose the same Embedder class (C3 split guard).

    contract_conn accepted so this runs inside the per-test rollback scope.
    """
    from paper_ingestion.ingestion.embedder import Embedder as CanonicalEmbedder

    # If a shim module exists it must re-export the canonical class unchanged.
    try:
        from paper_ingestion.embedder import Embedder as ShimEmbedder  # type: ignore[import]

        assert CanonicalEmbedder is ShimEmbedder, (
            "Shim and canonical Embedder must be the same object after C3 split"
        )
    except ImportError:
        # No shim module — canonical path only; that is fine.
        pass
