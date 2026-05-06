#!/usr/bin/env python3
"""Evaluate retrieval quality using verified key findings as ground truth.

Measures Precision@1 and Recall@3 by searching for each verified finding
and checking whether the correct paper's chunks are returned.

Run before and after model upgrade to compare:
    python -m scripts.eval_retrieval          # baseline with old model
    python scripts/eval_retrieval.py          # baseline with old model
    # ... upgrade model, run reembed.py ...
    python -m scripts.eval_retrieval          # comparison with new model
    python scripts/eval_retrieval.py          # comparison with new model

Environment variables:
    DATABASE_URL        - PostgreSQL connection string (or individual PG* vars)
    PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
    QDRANT_HOST         - Qdrant hostname (default: localhost)
    QDRANT_PORT         - Qdrant port (default: 6333)
    LITELLM_BASE_URL    - LiteLLM proxy URL (default: http://localhost:4000)
    EMBEDDING_MODEL     - LiteLLM model alias (default: embed)
    EMBEDDING_DIMENSION - Vector dimension (default: 1024)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import asyncpg
import httpx
from qdrant_client import AsyncQdrantClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class ScriptError(RuntimeError):
    """Script-level error; caught by the __main__ block."""


logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (
    _REPO_ROOT,
    _REPO_ROOT / "libs" / "jarvis_common",
    _REPO_ROOT / "services" / "paper_ingestion",
):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

if __package__:
    from scripts._db import get_dsn
else:
    try:
        from _db import get_dsn
    except ModuleNotFoundError:
        from scripts._db import get_dsn

from jarvis_common.llm_client import (
    embed_texts,
    get_litellm_config,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LITELLM_CONFIG = get_litellm_config(base_url_default="http://localhost:4000")
LITELLM_BASE_URL = LITELLM_CONFIG.base_url
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "embed")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "1024"))
EVAL_RETRIEVAL_SET = os.environ.get("EVAL_RETRIEVAL_SET")
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION_NAME = os.environ.get("EVAL_COLLECTION", "paper_chunks")
EVAL_OUTPUT_FILE = os.environ.get("EVAL_OUTPUT_FILE")


class EvalCase(NamedTuple):
    """One retrieval evaluation query and its relevant paper ids."""

    query: str
    expected_paper_ids: tuple[int, ...]
    source: str
    tags: tuple[str, ...] = ()


async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]:
    """Get embedding for a single text via LiteLLM."""
    embeddings = await embed_texts(
        client,
        [text],
        model=EMBEDDING_MODEL,
        timeout=60.0,
        config=LITELLM_CONFIG,
    )
    return embeddings[0]


async def search_qdrant(
    qdrant: AsyncQdrantClient,
    query_embedding: list[float],
    limit: int = 3,
) -> list[dict]:
    """Search Qdrant for similar chunks, returning paper_id and score."""
    response = await qdrant.query_points(
        collection_name=COLLECTION_NAME,
        query=query_embedding,
        limit=limit,
        with_payload=True,
    )
    return [
        {
            "paper_id": hit.payload.get("paper_id"),
            "chunk_index": hit.payload.get("chunk_index"),
            "score": hit.score,
            "content": (hit.payload.get("content") or "")[:100],
        }
        for hit in response.points
    ]


def extract_ground_truth(rows: list[asyncpg.Record | dict]) -> list[tuple[str, int]]:
    """Extract verified findings and their paper ids from summary rows."""
    ground_truth: list[tuple[str, int]] = []
    for row in rows:
        paper_id = row["paper_id"]
        raw_findings = row["key_findings"]
        if isinstance(raw_findings, str):
            try:
                findings = json.loads(raw_findings)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed key_findings payload for paper %s", paper_id)
                continue
        else:
            findings = raw_findings
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            text = finding.get("finding", "")
            verified = finding.get("verified") is True
            if isinstance(text, str) and text and verified:
                ground_truth.append((text, paper_id))
    return ground_truth


def _case_from_json_line(payload: dict, *, source: str) -> EvalCase:
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ScriptError(f"{source}: query must be a non-empty string")

    raw_expected = payload.get("expected_paper_ids")
    if raw_expected is None:
        raw_expected = [payload.get("expected_paper_id")]
    if not isinstance(raw_expected, list) or not raw_expected:
        raise ScriptError(f"{source}: expected_paper_ids must be a non-empty list")

    expected: list[int] = []
    for item in raw_expected:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ScriptError(f"{source}: expected paper ids must be integers")
        expected.append(item)

    raw_tags = payload.get("tags") or []
    if not isinstance(raw_tags, list) or not all(isinstance(tag, str) for tag in raw_tags):
        raise ScriptError(f"{source}: tags must be a string list when provided")

    return EvalCase(
        query=query.strip(),
        expected_paper_ids=tuple(expected),
        source=source,
        tags=tuple(raw_tags),
    )


def load_eval_set(path: str | Path) -> list[EvalCase]:
    """Load curated retrieval eval cases from JSONL."""
    eval_path = Path(path)
    cases: list[EvalCase] = []
    with eval_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScriptError(f"{eval_path.name}:{line_number}: invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ScriptError(f"{eval_path.name}:{line_number}: eval case must be an object")
            cases.append(_case_from_json_line(payload, source=f"{eval_path.name}:{line_number}"))
    if not cases:
        raise ScriptError(f"{eval_path}: no eval cases found")
    return cases


def precision_at_1(results: list[dict], expected_paper_ids: set[int]) -> float:
    """Return binary precision@1 for a single query."""
    if not results:
        return 0.0
    return 1.0 if results[0].get("paper_id") in expected_paper_ids else 0.0


def recall_at_k(results: list[dict], expected_paper_ids: set[int], k: int) -> float:
    """Return recall@k for a single query with one or more relevant papers."""
    if not expected_paper_ids:
        return 0.0
    retrieved = {result.get("paper_id") for result in results[:k]}
    return len(expected_paper_ids & retrieved) / len(expected_paper_ids)


def ndcg_at_k(results: list[dict], expected_paper_ids: set[int], k: int) -> float:
    """Return binary nDCG@k for a single query."""
    if not expected_paper_ids:
        return 0.0
    dcg = 0.0
    for rank, result in enumerate(results[:k], 1):
        if result.get("paper_id") in expected_paper_ids:
            dcg += 1.0 / math.log2(rank + 1)
    ideal_hits = min(len(expected_paper_ids), k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / ideal_dcg if ideal_dcg > 0 else 0.0


async def _load_cases(pool: asyncpg.Pool) -> tuple[list[EvalCase], str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ps.paper_id, ps.key_findings
               FROM paper_summaries ps
               WHERE ps.summary_verified = TRUE
                 AND ps.key_findings IS NOT NULL
                 AND ps.key_findings::text != '[]'"""
        )
    ground_truth = extract_ground_truth(rows)
    cases = [
        EvalCase(query=finding_text, expected_paper_ids=(paper_id,), source="verified finding")
        for finding_text, paper_id in ground_truth
    ]
    return cases, f"verified findings across {len(rows)} papers"


async def main() -> None:
    """Run the retrieval evaluation."""
    pool: asyncpg.Pool | None = None
    if EVAL_RETRIEVAL_SET:
        cases, source_label = load_eval_set(EVAL_RETRIEVAL_SET), "fixed eval set"
    else:
        pool = await asyncpg.create_pool(get_dsn(), min_size=1, max_size=3)
        if pool is None:
            raise ScriptError("Failed to create database pool")
        cases, source_label = await _load_cases(pool)

    if not cases:
        logger.info("No retrieval eval cases found. Cannot evaluate retrieval.")
        if pool is not None:
            await pool.close()
        return

    logger.info("Evaluating %d retrieval queries from %s", len(cases), source_label)

    qdrant = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY)
    p_at_1_hits = 0
    r_at_3_hits = 0
    ndcg_at_3_sum = 0.0
    total_latency_ms = 0.0
    total = len(cases)
    failed_queries = 0
    per_case_results: list[dict] = []

    # Table header
    print()
    print(f"Evaluating source: {source_label}")
    print(f"{'#':>4}  {'Papers':>12}  {'P@1':>4}  {'R@3':>4}  {'nDCG':>5}  {'ms':>7}  Query")
    print("-" * 80)

    async with httpx.AsyncClient() as http_client:
        for i, case in enumerate(cases, 1):
            expected = set(case.expected_paper_ids)
            started = time.perf_counter()
            try:
                query_emb = await embed_text(http_client, case.query)
                results = await search_qdrant(qdrant, query_emb, limit=3)

                p1 = precision_at_1(results, expected)
                p_at_1_hits += int(p1)
                r3 = recall_at_k(results, expected, 3)
                r_at_3_hits += r3
                ndcg3 = ndcg_at_k(results, expected, 3)
                ndcg_at_3_sum += ndcg3

                top_score = results[0]["score"] if results else 0.0
                query_short = case.query[:50] + ("..." if len(case.query) > 50 else "")
                ok1 = "Y" if p1 else "N"
                ok3 = f"{r3:.0%}" if 0.0 < r3 < 1.0 else ("Y" if r3 else "N")
                elapsed_ms = (time.perf_counter() - started) * 1000
                total_latency_ms += elapsed_ms
                per_case_results.append(
                    {
                        "query": case.query,
                        "expected_paper_ids": list(case.expected_paper_ids),
                        "p1": p1,
                        "r3": r3,
                        "ndcg3": ndcg3,
                        "latency_ms": elapsed_ms,
                        "top_result_paper_id": results[0]["paper_id"] if results else None,
                    }
                )
                print(  # noqa: E501
                    f"{i:4d}  {','.join(str(pid) for pid in case.expected_paper_ids):>12}"
                    f"  {ok1:>4}  {ok3:>4}  {ndcg3:5.2f}  {elapsed_ms:7.1f}  "
                    f"{query_short}  top={top_score:.3f}"
                )

            except Exception:
                logger.exception("Failed to evaluate query %d", i)
                failed_queries += 1

    # Summary
    print()
    print("=" * 80)
    denominator = total
    p_at_1 = p_at_1_hits / denominator if denominator > 0 else 0.0
    r_at_3 = r_at_3_hits / denominator if denominator > 0 else 0.0
    ndcg_at_3 = ndcg_at_3_sum / denominator if denominator > 0 else 0.0
    successful_queries = total - failed_queries
    avg_latency_ms = total_latency_ms / successful_queries if successful_queries > 0 else 0.0
    print(f"Precision@1:  {p_at_1:.1%}  ({p_at_1_hits}/{denominator})")
    print(f"Recall@3:     {r_at_3:.1%}  ({r_at_3_hits:g}/{denominator})")
    print(f"nDCG@3:       {ndcg_at_3:.1%}  ({ndcg_at_3_sum:g}/{denominator})")
    print(f"Average latency: {avg_latency_ms:.1f} ms/query")
    print(f"Total queries: {denominator}")
    print(f"Failed queries: {failed_queries}")
    print("=" * 80)

    if EVAL_OUTPUT_FILE:
        from datetime import UTC, datetime

        _reranker = os.environ.get("RERANKER_MODEL", "mixedbread-ai/mxbai-rerank-base-v2")
        output = {
            "run_at": datetime.now(UTC).isoformat(),
            "collection": COLLECTION_NAME,
            "reranker_model": _reranker,
            "eval_source": source_label,
            "total_cases": total,
            "failed_queries": failed_queries,
            "p_at_1": p_at_1,
            "r_at_3": r_at_3,
            "ndcg_at_3": ndcg_at_3,
            "mean_latency_ms": avg_latency_ms,
            "per_case": per_case_results,
        }
        Path(EVAL_OUTPUT_FILE).write_text(json.dumps(output, indent=2))
        print(f"Results written to {EVAL_OUTPUT_FILE}")

    if pool is not None:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
