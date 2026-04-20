#!/usr/bin/env python3
"""Re-embed paper chunks with a new embedding model.

Idempotent batch script: skips papers already embedded with the target model.
Meant to be run ONCE after switching the embedding model in LiteLLM config.

Usage:
    python -m scripts.reembed
    python scripts/reembed.py

Environment variables (reads from .env or system environment):
    DATABASE_URL        - PostgreSQL connection string (or individual PG* vars)
    PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
    QDRANT_HOST         - Qdrant hostname (default: localhost)
    QDRANT_PORT         - Qdrant port (default: 6333)
    LITELLM_BASE_URL    - LiteLLM proxy URL (default: http://localhost:4000)
    LITELLM_API_KEY     - LiteLLM API key (default: empty)
    EMBEDDING_MODEL     - LiteLLM model alias (default: embed)
    EMBEDDING_MODEL_NAME- Actual model name for tracking (default: nomic-embed-text)
    EMBEDDING_DIMENSION - Vector dimension (default: 768)
    REEMBED_BATCH_SIZE  - Papers per batch for progress logging (default: 5)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

import asyncpg
import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointIdsList, PointStruct

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class ScriptError(RuntimeError):
    """Script-level error; caught by the __main__ block."""


logger = logging.getLogger(__name__)

if __package__:
    from scripts._db import get_dsn
else:
    from _db import get_dsn

from jarvis_common.llm_client import (  # noqa: E402
    LiteLLMConfig,
    get_litellm_config,
)
from jarvis_common.llm_client import (  # noqa: E402
    embed_texts as embed_texts_shared,
)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

LITELLM_CONFIG = get_litellm_config(base_url_default="http://localhost:4000")
LITELLM_BASE_URL = LITELLM_CONFIG.base_url
LITELLM_API_KEY = LITELLM_CONFIG.api_key
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "embed")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "nomic-embed-text")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "768"))
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION_NAME = "paper_chunks"
BATCH_SIZE = int(os.environ.get("REEMBED_BATCH_SIZE", "5"))
EMBED_BATCH_SIZE = 32  # chunks per embedding API call


async def embed_texts(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """Get embeddings for a batch of texts via LiteLLM."""
    return await embed_texts_shared(
        client,
        texts,
        model=EMBEDDING_MODEL,
        timeout=120.0,
        config=LiteLLMConfig(
            base_url=LITELLM_BASE_URL,
            api_key=LITELLM_API_KEY,
        ),
    )


async def reembed_paper(
    paper_id: int,
    pool: asyncpg.Pool,
    qdrant: AsyncQdrantClient,
    http_client: httpx.AsyncClient,
) -> int:
    """Re-embed all chunks for a single paper. Returns chunk count."""
    # 1. Get chunks from DB
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, chunk_index, content, page_number, start_char, end_char,
                      embedding_id
               FROM paper_chunks
               WHERE paper_id = $1
               ORDER BY chunk_index""",
            paper_id,
        )

    if not rows:
        return 0

    # 2. Re-embed in batches
    all_new_ids: list[str] = []
    all_embeddings: list[list[float]] = []

    for i in range(0, len(rows), EMBED_BATCH_SIZE):
        batch = rows[i : i + EMBED_BATCH_SIZE]
        texts = [r["content"] for r in batch]
        embeddings = await embed_texts(http_client, texts)
        all_embeddings.extend(embeddings)

    if len(all_embeddings) != len(rows):
        logger.error(
            "Embedding count mismatch for paper %d: expected %d, got %d. Skipping.",
            paper_id,
            len(rows),
            len(all_embeddings),
        )
        return 0

    # 3. Upsert new points to Qdrant
    points: list[PointStruct] = []
    for row, embedding in zip(rows, all_embeddings):
        point_id = str(uuid.uuid4())
        all_new_ids.append(point_id)
        points.append(
            PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "paper_id": paper_id,
                    "chunk_index": row["chunk_index"],
                    "page_number": row["page_number"],
                    "content": row["content"],
                    "embedding_model": EMBEDDING_MODEL_NAME,
                },
            )
        )

    # Upsert in batches of 100 to avoid oversized requests
    for i in range(0, len(points), 100):
        await qdrant.upsert(
            collection_name=COLLECTION_NAME,
            points=points[i : i + 100],
        )

    # 4. Update DB first: set new embedding_id and embedding_model.
    #    If Qdrant upsert succeeded but DB update fails, a re-run will
    #    re-embed (old embedding_model still in DB) and orphaned Qdrant
    #    points are harmless.
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row, new_id in zip(rows, all_new_ids):
                await conn.execute(
                    """UPDATE paper_chunks
                       SET embedding_id = $1, embedding_model = $2
                       WHERE id = $3""",
                    new_id,
                    EMBEDDING_MODEL_NAME,
                    row["id"],
                )

    # 5. Delete old Qdrant points (after new ones are in place)
    old_point_ids = [r["embedding_id"] for r in rows if r["embedding_id"]]
    if old_point_ids:
        try:
            await qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=old_point_ids),
            )
        except Exception:
            logger.warning(
                "Failed to delete %d old Qdrant points for paper %d; "
                "orphaned points will not affect correctness",
                len(old_point_ids),
                paper_id,
                exc_info=True,
            )

    return len(rows)


async def main() -> None:
    """Run the re-embedding pipeline."""
    logger.info(
        "Starting re-embedding: target model=%s, LiteLLM=%s",
        EMBEDDING_MODEL_NAME,
        LITELLM_BASE_URL,
    )

    pool = await asyncpg.create_pool(get_dsn(), min_size=1, max_size=5)
    if pool is None:
        raise ScriptError("Failed to create database pool")

    qdrant = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY)

    # Find papers that need re-embedding
    async with pool.acquire() as conn:
        paper_ids = await conn.fetch(
            """SELECT DISTINCT paper_id
               FROM paper_chunks
               WHERE embedding_model != $1 OR embedding_model IS NULL
               ORDER BY paper_id""",
            EMBEDDING_MODEL_NAME,
        )

    total = len(paper_ids)
    if total == 0:
        logger.info("All papers already embedded with %s. Nothing to do.", EMBEDDING_MODEL_NAME)
        await pool.close()
        return

    logger.info("Found %d papers to re-embed", total)

    async with httpx.AsyncClient() as http_client:
        done = 0
        total_chunks = 0
        for i in range(0, total, BATCH_SIZE):
            batch = paper_ids[i : i + BATCH_SIZE]
            for record in batch:
                pid = record["paper_id"]
                try:
                    count = await reembed_paper(pid, pool, qdrant, http_client)
                    total_chunks += count
                    done += 1
                    logger.info("  [%d/%d] paper_id=%d  chunks=%d", done, total, pid, count)
                except Exception:
                    logger.exception("Failed to re-embed paper_id=%d, skipping", pid)
                    done += 1

            logger.info(
                "Batch progress: %d/%d papers done (%d chunks total)",
                done,
                total,
                total_chunks,
            )

    await pool.close()
    logger.info(
        "Re-embedding complete: %d papers, %d chunks processed with model=%s",
        done,
        total_chunks,
        EMBEDDING_MODEL_NAME,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
