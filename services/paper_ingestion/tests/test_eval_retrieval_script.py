"""Direct tests for the retrieval evaluation script."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_eval_retrieval(monkeypatch, **env):
    """Load the eval_retrieval script as a fresh module with controlled env vars."""
    project_root = Path(__file__).resolve().parents[3]
    script_path = project_root / "scripts" / "eval_retrieval.py"
    service_root = project_root / "services" / "paper_ingestion"
    monkeypatch.syspath_prepend(str(project_root))
    monkeypatch.syspath_prepend(str(service_root))
    monkeypatch.setitem(
        sys.modules,
        "qdrant_client",
        SimpleNamespace(AsyncQdrantClient=MagicMock(name="AsyncQdrantClient")),
    )

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    module_name = "test_eval_retrieval_module"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _pool_with_rows(rows):
    """Build a pool mock whose acquire() yields a connection with fetch()."""
    conn = AsyncMock()
    conn.fetch.return_value = rows

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    pool.close = AsyncMock()
    return pool


def _async_client_cm(client):
    """Build an async context manager that yields the provided client."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def test_default_embedding_dimension_matches_phase_c(monkeypatch):
    """eval_retrieval defaults to the Phase C embedding vector dimension."""
    monkeypatch.delenv("EMBEDDING_DIMENSION", raising=False)

    module = _load_eval_retrieval(monkeypatch)

    assert module.EMBEDDING_DIMENSION == 1024


@pytest.mark.asyncio
async def test_embed_text_calls_litellm_with_configured_url(monkeypatch):
    """embed_text sends the request to the configured LiteLLM endpoint (no auth headers)."""
    module = _load_eval_retrieval(
        monkeypatch,
        LITELLM_BASE_URL="http://litellm.test:4000",
        EMBEDDING_MODEL="embed-custom",
    )
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    client = AsyncMock()
    client.post.return_value = response

    embedding = await module.embed_text(client, "What did the paper show?")

    assert embedding == [0.1, 0.2, 0.3]
    client.post.assert_awaited_once_with(
        "http://litellm.test:4000/v1/embeddings",
        json={"model": "embed-custom", "input": ["What did the paper show?"]},
        headers={},
        timeout=60.0,
    )


@pytest.mark.asyncio
async def test_search_qdrant_returns_paper_and_chunk_metadata(monkeypatch):
    """search_qdrant flattens Qdrant hits into the expected script shape."""
    module = _load_eval_retrieval(monkeypatch)
    qdrant = AsyncMock()
    qdrant.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(
                payload={
                    "paper_id": 11,
                    "chunk_index": 4,
                    "content": "A" * 140,
                },
                score=0.91,
            ),
            SimpleNamespace(payload={"paper_id": 12, "chunk_index": 5}, score=0.73),
        ]
    )

    results = await module.search_qdrant(qdrant, [0.5, 0.1], limit=2)

    assert results == [
        {
            "paper_id": 11,
            "chunk_index": 4,
            "score": 0.91,
            "content": "A" * 100,
        },
        {
            "paper_id": 12,
            "chunk_index": 5,
            "score": 0.73,
            "content": "",
        },
    ]
    qdrant.query_points.assert_awaited_once_with(
        collection_name="paper_chunks",
        query=[0.5, 0.1],
        limit=2,
        with_payload=True,
    )


@pytest.mark.asyncio
async def test_main_ignores_unverified_findings_and_reports_metrics(monkeypatch, capsys):
    """main evaluates only verified findings and reports aggregate precision/recall."""
    module = _load_eval_retrieval(monkeypatch)
    rows = [
        {
            "paper_id": 101,
            "key_findings": json.dumps(
                [
                    {"finding": "Finding one", "verified": True},
                    {"finding": "Ignore me", "verified": False},
                    {"finding": "", "verified": True},
                ]
            ),
        },
        {
            "paper_id": 202,
            "key_findings": [{"finding": "Finding two", "verified": True}],
        },
    ]
    pool = _pool_with_rows(rows)
    http_client = AsyncMock()
    qdrant = MagicMock()

    monkeypatch.setattr(module.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(module, "AsyncQdrantClient", MagicMock(return_value=qdrant))
    monkeypatch.setattr(
        module.httpx, "AsyncClient", MagicMock(return_value=_async_client_cm(http_client))
    )
    monkeypatch.setattr(module, "embed_text", AsyncMock(side_effect=[[0.1], [0.2]]))
    monkeypatch.setattr(
        module,
        "search_qdrant",
        AsyncMock(
            side_effect=[
                [{"paper_id": 101, "chunk_index": 1, "score": 0.98, "content": "first"}],
                [
                    {"paper_id": 999, "chunk_index": 3, "score": 0.88, "content": "wrong"},
                    {"paper_id": 202, "chunk_index": 8, "score": 0.71, "content": "right"},
                ],
            ]
        ),
    )

    await module.main()

    output = capsys.readouterr().out
    assert "Precision@1:  50.0%  (1/2)" in output
    assert "Recall@3:     100.0%  (2/2)" in output
    assert "Total findings evaluated: 2" in output
    pool.close.assert_awaited_once()
    assert module.embed_text.await_count == 2
    assert module.search_qdrant.await_count == 2


@pytest.mark.asyncio
async def test_main_drops_failed_findings_from_denominator(monkeypatch, capsys):
    """main excludes failed evaluations from the final precision/recall denominator."""
    module = _load_eval_retrieval(monkeypatch)
    rows = [
        {"paper_id": 301, "key_findings": [{"finding": "Reliable finding", "verified": True}]},
        {"paper_id": 302, "key_findings": [{"finding": "Broken finding", "verified": True}]},
    ]
    pool = _pool_with_rows(rows)

    monkeypatch.setattr(module.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(module, "AsyncQdrantClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        MagicMock(return_value=_async_client_cm(AsyncMock())),
    )
    monkeypatch.setattr(
        module,
        "embed_text",
        AsyncMock(side_effect=[[0.1], RuntimeError("embedding offline")]),
    )
    monkeypatch.setattr(
        module,
        "search_qdrant",
        AsyncMock(
            return_value=[{"paper_id": 301, "chunk_index": 1, "score": 0.95, "content": "ok"}]
        ),
    )

    await module.main()

    output = capsys.readouterr().out
    assert "Precision@1:  100.0%  (1/1)" in output
    assert "Recall@3:     100.0%  (1/1)" in output
    assert "Total findings evaluated: 1" in output
    pool.close.assert_awaited_once()
