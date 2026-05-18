"""Backfill paper_chunks.user_id and Qdrant point payloads.

Stage 1: SQL — populate paper_chunks.user_id from papers.discovered_by where
         the chunk's user_id is NULL (post-Sprint-B canonical-corpus rename).
Stage 2: Python — scroll Qdrant by paper_id and set_payload({"user_id": ...})
         per point so the search-filter helper sees consistent payloads.

Both stages are idempotent: re-running re-sets identical values. The default
is --dry-run; pass --apply to actually mutate state.

Usage:
    python scripts/backfill_qdrant_user_id.py            # dry-run (default)
    python scripts/backfill_qdrant_user_id.py --apply    # write
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_dsn  # noqa: E402

COLLECTION_NAME = "paper_chunks"


def get_qdrant_url() -> str:
    """Return the Qdrant service URL from ``QDRANT_URL`` (default: ``http://localhost:6333``).

    Returns
    -------
    str
        Qdrant base URL.
    """
    return os.environ.get("QDRANT_URL", "http://localhost:6333")


async def stage1_sql(pool: asyncpg.Pool, *, dry_run: bool) -> int:
    """Populate ``paper_chunks.user_id`` from ``papers.discovered_by``.

    In dry-run mode returns the count of rows that *would* be updated without
    making any changes. In apply mode runs the UPDATE and returns the affected
    row count.

    Parameters
    ----------
    pool : asyncpg.Pool
        Database connection pool.
    dry_run : bool
        When ``True``, runs a COUNT query only (no writes).

    Returns
    -------
    int
        Number of rows updated (or that would be updated in dry-run mode).
    """
    async with pool.acquire() as conn:
        if dry_run:
            return await conn.fetchval(
                """SELECT count(*)
                     FROM paper_chunks pc
                     JOIN papers p ON pc.paper_id = p.id
                    WHERE pc.user_id IS NULL
                      AND p.discovered_by IS NOT NULL"""
            )
        result = await conn.execute(
            """UPDATE paper_chunks
                  SET user_id = p.discovered_by
                 FROM papers p
                WHERE paper_chunks.paper_id = p.id
                  AND paper_chunks.user_id IS NULL
                  AND p.discovered_by IS NOT NULL"""
        )
        return int(result.split()[-1])


async def stage2_qdrant(
    pool: asyncpg.Pool, qdrant: AsyncQdrantClient, *, dry_run: bool
) -> tuple[int, int]:
    """Stamp ``user_id`` payload on every existing Qdrant point for papers with chunks.

    Scrolls all points in the ``paper_chunks`` Qdrant collection by paper and
    calls ``set_payload`` for each batch (no-op in dry-run mode).

    Parameters
    ----------
    pool : asyncpg.Pool
        Database connection pool used to resolve ``papers.discovered_by``.
    qdrant : AsyncQdrantClient
        Qdrant async client.
    dry_run : bool
        When ``True``, scrolls points but skips ``set_payload`` writes.

    Returns
    -------
    tuple[int, int]
        ``(papers_processed, total_points_visited)``.
    """
    async with pool.acquire() as conn:
        papers = await conn.fetch(
            """SELECT id, discovered_by
                 FROM papers
                WHERE id IN (SELECT DISTINCT paper_id FROM paper_chunks)"""
        )

    papers_done = points_done = 0
    for paper in papers:
        scroll_offset: int | str | None = None
        while True:
            points, next_offset = await qdrant.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=Filter(
                    must=[FieldCondition(key="paper_id", match=MatchValue(value=paper["id"]))]
                ),
                limit=256,
                offset=scroll_offset,
                with_payload=False,
            )
            if not points:
                break
            if not dry_run:
                await qdrant.set_payload(
                    collection_name=COLLECTION_NAME,
                    payload={"user_id": paper["discovered_by"]},
                    points=[pt.id for pt in points],
                )
            points_done += len(points)
            if next_offset is None:
                break
            scroll_offset = next_offset
        papers_done += 1
    return papers_done, points_done


async def main() -> None:
    """Parse CLI arguments and run Stage 1 (SQL) then Stage 2 (Qdrant) backfill."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script runs in dry-run mode.",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    pool = await asyncpg.create_pool(get_dsn())
    qdrant = AsyncQdrantClient(url=get_qdrant_url())
    try:
        rows = await stage1_sql(pool, dry_run=dry_run)
        verb = "would update" if dry_run else "updated"
        print(f"Stage 1 (SQL): {verb} {rows} paper_chunks row(s)")

        papers, points = await stage2_qdrant(pool, qdrant, dry_run=dry_run)
        verb = "would patch" if dry_run else "patched"
        print(f"Stage 2 (Qdrant): {verb} {points} point(s) across {papers} paper(s)")

        if dry_run:
            print("\nDry run only. Re-run with --apply to commit.")
    finally:
        await pool.close()
        await qdrant.close()


if __name__ == "__main__":
    asyncio.run(main())
