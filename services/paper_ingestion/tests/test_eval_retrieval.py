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

    assert results == [
        {
            "paper_id": 42,
            "chunk_index": 7,
            "score": 0.91,
            "content": "x" * 100,
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
