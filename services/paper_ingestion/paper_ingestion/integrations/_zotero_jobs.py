"""Procrastinate job handlers for the five ``zotero.*`` kinds."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx
from jarvis_common.advisory_lock import AdvisoryLock, _kind_lock_key
from jarvis_common.jobs import ProgressContext

from paper_ingestion.integrations._zotero_highlights import (
    push_highlights_for_paper,
    sync_annotations_for_paper,
)
from paper_ingestion.integrations._zotero_poll import poll_zotero_library
from paper_ingestion.integrations._zotero_push import (
    push_paper_to_zotero,
    resync_paper_to_zotero,
)

logger = logging.getLogger("paper_ingestion.integrations.zotero_service")


async def _revalidate_paper_ownership(pool: asyncpg.Pool, payload: dict[str, Any]) -> int:
    """Re-validate ownership at job execution time to prevent IDOR via queued jobs."""
    from jarvis_common.db_helpers import assert_paper_ownership

    paper_id = int(payload["paper_id"])
    user_id = payload.get("user_id")
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    return paper_id


async def _zotero_push_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Job handler for zotero.push — push a single paper to Zotero.

    Payload keys:
        paper_id (int): DB paper ID to push.
        user_id (int | None): Caller user ID for ownership check.
    """
    paper_id = await _revalidate_paper_ownership(pool, payload)
    user_id = payload.get("user_id")

    await ctx.update_progress(0.1, "Starting Zotero push")
    await push_paper_to_zotero(paper_id, pool, http_client, owner_user_id=user_id)
    await ctx.update_progress(1.0, "Done")
    return {"paper_id": paper_id, "status": "pushed"}


async def _zotero_resync_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Job handler for zotero.resync — force re-push a paper to Zotero.

    Payload keys:
        paper_id (int): DB paper ID to resync.
        user_id (int | None): Caller user ID for ownership check.
    """
    paper_id = await _revalidate_paper_ownership(pool, payload)
    user_id = payload.get("user_id")

    await ctx.update_progress(0.1, "Clearing existing Zotero key")
    await resync_paper_to_zotero(paper_id, pool, http_client, owner_user_id=user_id)
    await ctx.update_progress(1.0, "Done")
    return {"paper_id": paper_id, "status": "resynced"}


async def _zotero_sync_from_zotero_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Job handler for zotero.sync_from_zotero — incremental library poll.

    Polls the Zotero library for items added since the last known version and
    enqueues paper.process jobs for any new items not originating in JARVIS.

    Serialised per user by a session advisory lock: the hourly cron and the
    manual ``POST /api/zotero/poll`` defer the same kind, and two concurrent
    polls of one library re-import the same items.  A duplicate returns without
    contacting Zotero at all.
    """
    # Thread caller user_id through so imported papers/state/annotations
    # are attributed correctly. NULL when scheduler-cron-invoked (system poll).
    polling_user_id = payload.get("user_id")
    lock_key2 = int(polling_user_id) if polling_user_id is not None else 0
    async with AdvisoryLock(
        pool, key1=_kind_lock_key("zotero.sync_from_zotero"), key2=lock_key2
    ) as locked:
        if not locked:
            await ctx.update_progress(
                1.0, "A Zotero sync was already in progress; this duplicate did nothing."
            )
            return {"status": "blocked", "reason": "Zotero sync already running"}
        await ctx.update_progress(0.1, "Starting Zotero library poll")
        result = await poll_zotero_library(pool, http_client, polling_user_id=polling_user_id)
    status = str(result.get("status", "error"))
    headline = "Done" if status == "ok" else status.replace("_", " ").title()
    await ctx.update_progress(1.0, headline)
    return result


async def _zotero_sync_annotations_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Job handler for importing Zotero annotations for a linked paper.

    Payload keys:
        paper_id (int): DB paper ID to sync annotations for.
        user_id (int | None): Caller user ID for ownership check.
    """
    paper_id = await _revalidate_paper_ownership(pool, payload)
    user_id = payload.get("user_id")

    await ctx.update_progress(0.1, "Fetching Zotero annotations")
    result = await sync_annotations_for_paper(
        paper_id,
        pool,
        http_client,
        owner_user_id=user_id,
    )
    await ctx.update_progress(1.0, "Done")
    return result


async def _zotero_push_highlights_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Job handler for zotero.push_highlights — export a paper's highlights to Zotero.

    Payload keys:
        paper_id (int): DB paper ID whose unsynced highlights are exported.
        user_id (int | None): Caller user ID for the view-level access check.
    """
    from paper_ingestion.routers.pdf_files import assert_paper_pdf_visible

    paper_id = int(payload["paper_id"])
    user_id = payload.get("user_id")

    # Re-validate view-level access at job execution time to prevent IDOR via
    # queued jobs. Mirrors the create/list-highlights authz (view => annotate =>
    # export); the export reads and pushes only the caller's own highlights
    # (push_highlights_for_paper: WHERE user_id = owner_user_id), so view-level
    # access cannot expose another user's highlights. ``user_id`` is None only in
    # single-user mode (no per-user isolation), where the prior ownership check
    # was likewise a no-op — so the visibility check is skipped there too.
    if user_id is not None:
        async with pool.acquire() as conn:
            await assert_paper_pdf_visible(conn, paper_id, user_id)

    await ctx.update_progress(0.1, "Exporting highlights to Zotero")
    result = await push_highlights_for_paper(
        paper_id,
        pool,
        http_client,
        owner_user_id=user_id,
    )
    await ctx.update_progress(1.0, "Done")
    return result
