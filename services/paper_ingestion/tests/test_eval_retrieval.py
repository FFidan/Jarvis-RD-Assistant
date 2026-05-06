"""Direct tests for the retrieval evaluation script."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# scripts/ lives at the repo root, which is not in pytest's pythonpath.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _load_module():
    """Reload the evaluation script after test-specific monkeypatching."""
    import importlib

    fake_qdrant = types.ModuleType("qdrant_client")
    fake_qdrant.AsyncQdrantClient = MagicMock()

    with patch.dict(sys.modules, {"qdrant_client": fake_qdrant}):
        import scripts.eval_retrieval as eval_mod

        return importlib.reload(eval_mod)


def _make_pool(rows):
    """Create a mock asyncpg pool for eval_retrieval.main."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    pool.close = AsyncMock()
    return pool


def _http_client_cm():
    """Create a mock httpx.AsyncClient context manager."""
    client = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return client, ctx


def test_extract_ground_truth_filters_invalid_findings():
    """extract_ground_truth keeps only verified findings with non-empty text."""
    eval_mod = _load_module()

    rows = [
        {
            "paper_id": 10,
            "key_findings": [
                {"finding": "Keep me", "verified": True},
                {"finding": "Drop me", "verified": False},
                {"finding": "", "verified": True},
                "not-a-dict",
            ],
        },
        {
            "paper_id": 11,
            "key_findings": '[{"finding": "From JSON", "verified": true}]',
        },
    ]

    assert eval_mod.extract_ground_truth(rows) == [
        ("Keep me", 10),
        ("From JSON", 11),
    ]


def test_extract_ground_truth_requires_explicit_verified_flag():
    """extract_ground_truth should ignore legacy findings missing an explicit verified flag."""
    eval_mod = _load_module()

    rows = [{"paper_id": 10, "key_findings": [{"finding": "Legacy finding"}]}]

    assert eval_mod.extract_ground_truth(rows) == []


def test_extract_ground_truth_skips_malformed_json():
    """extract_ground_truth skips malformed JSON payloads instead of aborting the run."""
    eval_mod = _load_module()

    rows = [
        {"paper_id": 10, "key_findings": "not-json"},
        {"paper_id": 11, "key_findings": [{"finding": "Keep me", "verified": True}]},
    ]

    assert eval_mod.extract_ground_truth(rows) == [("Keep me", 11)]


@pytest.mark.asyncio
async def test_search_qdrant_maps_payload_fields():
    """search_qdrant projects the payload fields used in printed results."""
    eval_mod = _load_module()
    response = SimpleNamespace(
        points=[
            SimpleNamespace(
                score=0.91,
                payload={
                    "paper_id": 42,
                    "chunk_index": 7,
                    "content": "x" * 120,
                },
            )
        ]
    )
    qdrant = AsyncMock()
    qdrant.query_points.return_value = response

    results = await eval_mod.search_qdrant(qdrant, [0.1, 0.2], limit=5)

    # D6-A removed the [:100] content truncation so rerankers see full passage text.
    assert results == [
        {
            "paper_id": 42,
            "chunk_index": 7,
            "score": 0.91,
            "content": "x" * 120,
        }
    ]
    qdrant.query_points.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_prints_retrieval_metrics(capsys):
    """main prints summary metrics for verified findings across two papers."""
    eval_mod = _load_module()
    rows = [
        {
            "paper_id": 1,
            "key_findings": [{"finding": "alpha", "verified": True}],
        },
        {
            "paper_id": 2,
            "key_findings": [{"finding": "beta", "verified": True}],
        },
    ]
    pool = _make_pool(rows)
    _, http_ctx = _http_client_cm()

    with (
        patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
        patch.object(eval_mod, "get_dsn", return_value="postgresql://test"),
        patch.object(eval_mod, "AsyncQdrantClient", return_value=MagicMock()),
        patch.object(eval_mod.httpx, "AsyncClient", return_value=http_ctx),
        patch.object(eval_mod, "embed_text", AsyncMock(return_value=[0.1] * 3)),
        patch.object(
            eval_mod,
            "search_qdrant",
            AsyncMock(
                side_effect=[
                    [{"paper_id": 1, "chunk_index": 0, "score": 0.9, "content": "alpha"}],
                    [
                        {"paper_id": 99, "chunk_index": 0, "score": 0.6, "content": "noise"},
                        {"paper_id": 2, "chunk_index": 1, "score": 0.55, "content": "beta"},
                    ],
                ]
            ),
        ),
    ):
        await eval_mod.main()

    output = capsys.readouterr().out
    assert "Precision@1:  50.0%  (1/2)" in output
    assert "Recall@3:     100.0%  (2/2)" in output
    assert "Total queries: 2" in output
    assert "Failed queries: 0" in output
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_returns_early_when_no_verified_findings():
    """main exits cleanly when the verified summary rows contain no usable findings."""
    eval_mod = _load_module()
    rows = [{"paper_id": 1, "key_findings": [{"finding": "alpha", "verified": False}]}]
    pool = _make_pool(rows)

    with (
        patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
        patch.object(eval_mod, "get_dsn", return_value="postgresql://test"),
    ):
        await eval_mod.main()

    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_exits_when_pool_creation_fails():
    """main should exit with status 1 when asyncpg cannot create a pool."""
    eval_mod = _load_module()

    with patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=None)):
        with pytest.raises(eval_mod.ScriptError, match="Failed to create database pool"):
            await eval_mod.main()


@pytest.mark.asyncio
async def test_main_reports_all_failed_findings_without_crashing(capsys):
    """main reports zero evaluated findings when every retrieval attempt fails."""
    eval_mod = _load_module()
    rows = [
        {"paper_id": 1, "key_findings": [{"finding": "alpha", "verified": True}]},
        {"paper_id": 2, "key_findings": [{"finding": "beta", "verified": True}]},
    ]
    pool = _make_pool(rows)
    _, http_ctx = _http_client_cm()

    with (
        patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
        patch.object(eval_mod, "get_dsn", return_value="postgresql://test"),
        patch.object(eval_mod, "AsyncQdrantClient", return_value=MagicMock()),
        patch.object(eval_mod.httpx, "AsyncClient", return_value=http_ctx),
        patch.object(eval_mod, "embed_text", AsyncMock(side_effect=RuntimeError("offline"))),
    ):
        await eval_mod.main()

    output = capsys.readouterr().out
    assert "Total queries: 2" in output
    assert "Failed queries: 2" in output


@pytest.mark.asyncio
async def test_main_uses_no_reranker_by_default(monkeypatch):
    """When EVAL_RERANKER is unset, neither reranker factory is called."""
    monkeypatch.delenv("EVAL_RERANKER", raising=False)
    eval_mod = _load_module()

    rows = [{"paper_id": 1, "key_findings": [{"finding": "alpha", "verified": True}]}]
    pool = _make_pool(rows)
    _, http_ctx = _http_client_cm()

    mock_get_reranker = MagicMock(return_value=None)
    mock_get_qwen3 = MagicMock(return_value=None)

    with (
        patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
        patch.object(eval_mod, "get_dsn", return_value="postgresql://test"),
        patch.object(eval_mod, "AsyncQdrantClient", return_value=MagicMock()),
        patch.object(eval_mod.httpx, "AsyncClient", return_value=http_ctx),
        patch.object(eval_mod, "embed_text", AsyncMock(return_value=[0.1] * 3)),
        patch.object(
            eval_mod,
            "search_qdrant",
            AsyncMock(
                return_value=[{"paper_id": 1, "chunk_index": 0, "score": 0.9, "content": "alpha"}]
            ),
        ),
        patch("paper_ingestion.ingestion.reranker.get_reranker", mock_get_reranker),
        patch(
            "paper_ingestion.ingestion.qwen3_reranker.get_qwen3_reranker",
            mock_get_qwen3,
        ),
    ):
        await eval_mod.main()

    mock_get_reranker.assert_not_called()
    mock_get_qwen3.assert_not_called()


@pytest.mark.asyncio
async def test_main_uses_mxbai_reranker_when_configured(monkeypatch, capsys):
    """With EVAL_RERANKER=mxbai the mxbai factory is called and its results used."""
    monkeypatch.setenv("EVAL_RERANKER", "mxbai")
    eval_mod = _load_module()

    rows = [
        {"paper_id": 1, "key_findings": [{"finding": "alpha", "verified": True}]},
        {"paper_id": 2, "key_findings": [{"finding": "beta", "verified": True}]},
    ]
    pool = _make_pool(rows)
    _, http_ctx = _http_client_cm()

    # Reranker mock: returns passages in the same order (identity rerank).
    mock_reranker = MagicMock()
    mock_reranker.rerank.side_effect = lambda query, passages, top_k=10: [
        (i, float(len(passages) - i)) for i in range(len(passages))
    ]
    mock_get_reranker = MagicMock(return_value=mock_reranker)

    with (
        patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
        patch.object(eval_mod, "get_dsn", return_value="postgresql://test"),
        patch.object(eval_mod, "AsyncQdrantClient", return_value=MagicMock()),
        patch.object(eval_mod.httpx, "AsyncClient", return_value=http_ctx),
        patch.object(eval_mod, "embed_text", AsyncMock(return_value=[0.1] * 3)),
        patch.object(
            eval_mod,
            "search_qdrant",
            AsyncMock(
                side_effect=[
                    [{"paper_id": 1, "chunk_index": 0, "score": 0.9, "content": "alpha"}],
                    [{"paper_id": 2, "chunk_index": 0, "score": 0.8, "content": "beta"}],
                ]
            ),
        ),
        patch.object(eval_mod, "get_reranker", mock_get_reranker),
    ):
        await eval_mod.main()

    mock_get_reranker.assert_called_once()
    output = capsys.readouterr().out
    assert "Total queries: 2" in output


@pytest.mark.asyncio
async def test_main_uses_qwen3_reranker_when_configured(monkeypatch, capsys):
    """With EVAL_RERANKER=qwen3-reranker the qwen3 factory is called."""
    monkeypatch.setenv("EVAL_RERANKER", "qwen3-reranker")
    eval_mod = _load_module()

    rows = [
        {"paper_id": 1, "key_findings": [{"finding": "alpha", "verified": True}]},
        {"paper_id": 2, "key_findings": [{"finding": "beta", "verified": True}]},
    ]
    pool = _make_pool(rows)
    _, http_ctx = _http_client_cm()

    mock_reranker = MagicMock()
    mock_reranker.rerank.side_effect = lambda query, passages, top_k=10: [
        (i, float(len(passages) - i)) for i in range(len(passages))
    ]
    mock_get_qwen3 = MagicMock(return_value=mock_reranker)

    with (
        patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
        patch.object(eval_mod, "get_dsn", return_value="postgresql://test"),
        patch.object(eval_mod, "AsyncQdrantClient", return_value=MagicMock()),
        patch.object(eval_mod.httpx, "AsyncClient", return_value=http_ctx),
        patch.object(eval_mod, "embed_text", AsyncMock(return_value=[0.1] * 3)),
        patch.object(
            eval_mod,
            "search_qdrant",
            AsyncMock(
                side_effect=[
                    [{"paper_id": 1, "chunk_index": 0, "score": 0.9, "content": "alpha"}],
                    [{"paper_id": 2, "chunk_index": 0, "score": 0.8, "content": "beta"}],
                ]
            ),
        ),
        patch.object(eval_mod, "get_qwen3_reranker", mock_get_qwen3),
    ):
        await eval_mod.main()

    mock_get_qwen3.assert_called_once()
    output = capsys.readouterr().out
    assert "Total queries: 2" in output


@pytest.mark.asyncio
async def test_main_passes_rerank_k_to_search_qdrant(monkeypatch, capsys):
    """EVAL_RERANK_K controls the limit passed to search_qdrant."""
    monkeypatch.setenv("EVAL_RERANKER", "mxbai")
    monkeypatch.setenv("EVAL_RERANK_K", "7")
    eval_mod = _load_module()

    rows = [{"paper_id": 1, "key_findings": [{"finding": "alpha", "verified": True}]}]
    pool = _make_pool(rows)
    _, http_ctx = _http_client_cm()

    mock_reranker = MagicMock()
    mock_reranker.rerank.side_effect = lambda query, passages, top_k=10: [
        (i, float(len(passages) - i)) for i in range(min(top_k, len(passages)))
    ]
    mock_get_reranker = MagicMock(return_value=mock_reranker)

    search_mock = AsyncMock(
        return_value=[{"paper_id": 1, "chunk_index": 0, "score": 0.9, "content": "alpha"}]
    )

    with (
        patch.object(eval_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
        patch.object(eval_mod, "get_dsn", return_value="postgresql://test"),
        patch.object(eval_mod, "AsyncQdrantClient", return_value=MagicMock()),
        patch.object(eval_mod.httpx, "AsyncClient", return_value=http_ctx),
        patch.object(eval_mod, "embed_text", AsyncMock(return_value=[0.1] * 3)),
        patch.object(eval_mod, "search_qdrant", search_mock),
        patch.object(eval_mod, "get_reranker", mock_get_reranker),
    ):
        await eval_mod.main()

    # search_qdrant must have been called with limit=7
    call_kwargs_list = search_mock.call_args_list
    assert len(call_kwargs_list) >= 1
    for c in call_kwargs_list:
        _, kw = c
        assert kw.get("limit") == 7, (
            f"Expected search_qdrant(limit=7) but got limit={kw.get('limit')}"
        )
