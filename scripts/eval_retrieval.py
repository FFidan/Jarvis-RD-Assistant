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
    LITELLM_API_KEY     - LiteLLM API key (default: empty)
    EMBEDDING_MODEL     - LiteLLM model alias (default: embed)
    EMBEDDING_DIMENSION - Vector dimension (default: 768)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import asyncpg
import httpx
from qdrant_client import AsyncQdrantClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)
if __package__:
    from scripts._db import get_dsn
    from scripts._paper_ingestion_imports import (
        LiteLLMConfig,
        embed_texts,
        get_litellm_config,
    )
else:
    from _db import get_dsn
    from _paper_ingestion_imports import (
        LiteLLMConfig,
        embed_texts,
        get_litellm_config,
    )

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LITELLM_CONFIG = get_litellm_config(base_url_default="http://localhost:4000")
LITELLM_BASE_URL = LITELLM_CONFIG.base_url
LITELLM_API_KEY = LITELLM_CONFIG.api_key
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "embed")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "768"))
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION_NAME = "paper_chunks"

async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]:
    """Get embedding for a single text via LiteLLM."""
    embeddings = await embed_texts(
        client,
        [text],
        model=EMBEDDING_MODEL,
        timeout=60.0,
        config=LiteLLMConfig(
            base_url=LITELLM_BASE_URL,
            api_key=LITELLM_API_KEY,
        ),
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


async def main() -> None:
    """Run the retrieval evaluation."""
    pool = await asyncpg.create_pool(get_dsn(), min_size=1, max_size=3)
    if pool is None:
        logger.error("Failed to create database pool")
        sys.exit(1)

    # Fetch verified key findings as ground truth
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ps.paper_id, ps.key_findings
               FROM paper_summaries ps
               WHERE ps.summary_verified = TRUE
                 AND ps.key_findings IS NOT NULL
                 AND ps.key_findings::text != '[]'"""
        )

    if not rows:
        logger.info("No verified findings found. Cannot evaluate retrieval.")
        await pool.close()
        return

    ground_truth = extract_ground_truth(rows)

    if not ground_truth:
        logger.info("No verified findings extracted. Cannot evaluate retrieval.")
        await pool.close()
        return

    logger.info("Evaluating %d verified findings across %d papers", len(ground_truth), len(rows))

    qdrant = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY)
    p_at_1_hits = 0
    r_at_3_hits = 0
    total = len(ground_truth)
    failed_findings = 0

    # Table header
    print()
    print(f"{'#':>4}  {'Paper':>6}  {'P@1':>4}  {'R@3':>4}  {'Score':>6}  Finding")
    print("-" * 80)

    async with httpx.AsyncClient() as http_client:
        for i, (finding_text, expected_pid) in enumerate(ground_truth, 1):
            try:
                query_emb = await embed_text(http_client, finding_text)
                results = await search_qdrant(qdrant, query_emb, limit=3)

                # P@1: is the top result from the expected paper?
                p1 = 1 if results and results[0]["paper_id"] == expected_pid else 0
                p_at_1_hits += p1

                # R@3: is the expected paper in the top 3 results?
                top3_pids = [r["paper_id"] for r in results]
                r3 = 1 if expected_pid in top3_pids else 0
                r_at_3_hits += r3

                top_score = results[0]["score"] if results else 0.0
                finding_short = finding_text[:50] + ("..." if len(finding_text) > 50 else "")
                ok1 = "Y" if p1 else "N"
                ok3 = "Y" if r3 else "N"
                print(  # noqa: E501
                    f"{i:4d}  {expected_pid:6d}  {ok1:>4}  {ok3:>4}"
                    f"  {top_score:6.3f}  {finding_short}"
                )

            except Exception:
                logger.exception("Failed to evaluate finding %d", i)
                failed_findings += 1

    evaluated_total = total - failed_findings

    # Summary
    print()
    print("=" * 80)
    p_at_1 = p_at_1_hits / evaluated_total if evaluated_total > 0 else 0.0
    r_at_3 = r_at_3_hits / evaluated_total if evaluated_total > 0 else 0.0
    print(f"Precision@1:  {p_at_1:.1%}  ({p_at_1_hits}/{evaluated_total})")
    print(f"Recall@3:     {r_at_3:.1%}  ({r_at_3_hits}/{evaluated_total})")
    print(f"Total findings evaluated: {evaluated_total}")
    print(f"Failed findings skipped: {failed_findings}")
    print("=" * 80)

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
