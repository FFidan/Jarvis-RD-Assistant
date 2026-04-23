"""Zotero push service — business logic and job handlers."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx
from jarvis_common import jobs as jobs_lib
from jarvis_common.jobs import JobContext, job_handler

from paper_ingestion.models.papers import PaperCreate, SourceType
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger(__name__)


async def _get_zotero_config(db_pool: asyncpg.Pool) -> dict[str, Any]:
    """Read Zotero settings from user_config. Returns dict with short keys."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM user_config WHERE key LIKE 'zotero.%'")
    config: dict[str, Any] = {}
    for row in rows:
        short_key = row["key"][len("zotero.") :]
        val = row["value"]
        # user_config values are stored as JSONB — asyncpg auto-decodes objects/arrays/booleans,
        # but scalar strings come back as str; numbers as int/float; booleans as bool.
        # No manual json.loads() needed (see CLAUDE.md asyncpg JSONB note).
        config[short_key] = val
    return config


async def push_paper_to_zotero(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
) -> None:
    """Push a paper to Zotero. Only pushes papers that have at least one project link.

    Push flow:
    1. Load config — skip if Zotero is disabled or credentials are missing.
    2. Load paper + project links — skip if no projects linked.
    3. Deduplicate by DOI against existing Zotero items.
    4. Create Zotero item with collections per linked project.
    5. Store zotero_item_key + zotero_last_pushed_at in papers table.
    6. Best-effort fetch of Better BibTeX citation key.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    cfg = await _get_zotero_config(db_pool)
    if not cfg.get("enabled"):
        logger.debug("Zotero disabled, skipping push for paper %d", paper_id)
        return

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    if not api_key or not user_id:
        logger.warning(
            "Zotero API key or user_id not configured, skipping push for paper %d", paper_id
        )
        return

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),
        http_client=http_client,
    )

    async with db_pool.acquire() as conn:
        paper = await conn.fetchrow(
            """
            SELECT p.id, p.title, p.authors, p.metadata->>'doi' AS doi, p.url, p.abstract,
                   p.pdf_local_path, p.zotero_item_key,
                   array_agg(DISTINCT pp.project_id)
                       FILTER (WHERE pp.project_id IS NOT NULL) AS project_ids
            FROM papers p
            LEFT JOIN project_papers pp ON pp.paper_id = p.id
            WHERE p.id = $1
            GROUP BY p.id
            """,
            paper_id,
        )

    if not paper:
        logger.warning("Zotero push: paper %d not found", paper_id)
        return

    project_ids: list[int] = list(paper["project_ids"] or [])
    if not project_ids:
        logger.info("Zotero push skipped: paper %d has no project links", paper_id)
        return

    # Already pushed: skip unless resync requested (resync clears the key first).
    if paper["zotero_item_key"]:
        logger.debug(
            "Paper %d already in Zotero (%s), skipping", paper_id, paper["zotero_item_key"]
        )
        return

    # DOI deduplication — reuse existing Zotero item if found.
    zotero_key: str | None = None
    if paper["doi"]:
        try:
            existing_item = await client.search_by_doi(paper["doi"])
            if existing_item:
                zotero_key = existing_item["key"]
                logger.info(
                    "Paper %d already in Zotero by DOI, reusing key %s", paper_id, zotero_key
                )
        except Exception:
            logger.warning("Zotero DOI search failed for paper %d", paper_id, exc_info=True)

    if zotero_key is None:
        # Build creators list from authors field (list of strings or dicts).
        authors: list[Any] = paper["authors"] or []
        creators: list[dict[str, str]] = []
        for author in authors:
            if isinstance(author, str):
                parts = author.rsplit(" ", 1)
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": parts[0] if len(parts) > 1 else "",
                        "lastName": parts[-1],
                    }
                )
            elif isinstance(author, dict):
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": author.get("firstName", author.get("first_name", "")),
                        "lastName": author.get("lastName", author.get("last_name", "")),
                    }
                )

        # Fetch topics as Zotero tags.
        async with db_pool.acquire() as conn:
            topic_rows = await conn.fetch(
                "SELECT t.name FROM topics t"
                " JOIN paper_topics pt ON pt.topic_id = t.id"
                " WHERE pt.paper_id = $1",
                paper_id,
            )
        tags = [{"tag": row["name"]} for row in topic_rows]

        item_data: dict[str, Any] = {
            "itemType": "journalArticle",
            "title": paper["title"] or "",
            "creators": creators,
            "DOI": paper["doi"] or "",
            "url": paper["url"] or "",
            "abstractNote": paper["abstract"] or "",
            "tags": tags,
            "extra": f"jarvis_paper_id={paper_id}",
            "collections": [],
        }

        # Resolve / create a Zotero collection for each linked project.
        collection_keys: list[str] = []
        for project_id in project_ids:
            try:
                async with db_pool.acquire() as conn:
                    project = await conn.fetchrow(
                        "SELECT id, name, zotero_collection_key FROM projects WHERE id = $1",
                        project_id,
                    )
                if not project:
                    continue
                if project["zotero_collection_key"]:
                    col_key = project["zotero_collection_key"]
                else:
                    col_key = await client.ensure_collection(project["name"])
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE projects SET zotero_collection_key = $1 WHERE id = $2",
                            col_key,
                            project_id,
                        )
                collection_keys.append(col_key)
            except Exception:
                logger.warning(
                    "Zotero collection setup failed for project %d", project_id, exc_info=True
                )

        item_data["collections"] = collection_keys

        try:
            result = await client.create_item(item_data)
            zotero_key = result.get("successful", {}).get("0", {}).get("key")
            if not zotero_key:
                logger.error(
                    "Zotero push failed for paper %d: no key in response %s", paper_id, result
                )
                return
        except Exception:
            logger.error("Zotero push failed for paper %d", paper_id, exc_info=True)
            raise

    # Persist Zotero item key.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE papers SET zotero_item_key = $1, zotero_last_pushed_at = NOW() WHERE id = $2",
            zotero_key,
            paper_id,
        )

    # Best-effort: fetch Better BibTeX citation key from local BBT plugin.
    try:
        bbt_key = await client.fetch_bbt_citation_key(zotero_key)
        if bbt_key:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE papers SET zotero_citation_key = $1 WHERE id = $2",
                    bbt_key,
                    paper_id,
                )
    except Exception:
        logger.debug("BBT citation key fetch failed for paper %d (non-fatal)", paper_id)

    logger.info("Paper %d pushed to Zotero: item_key=%s", paper_id, zotero_key)


async def resync_paper_to_zotero(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
) -> None:
    """Force re-push paper to Zotero (clears existing zotero_item_key first)."""
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE papers SET zotero_item_key = NULL WHERE id = $1", paper_id)
    await push_paper_to_zotero(paper_id, db_pool, http_client)


async def poll_zotero_library(
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Incremental poll of Zotero library since last known version.

    For each new item:
    - If Extra field contains 'jarvis_paper_id=' → skip (originated in JARVIS)
    - If DOI matches existing JARVIS paper → link zotero_item_key (skip ingestion)
    - Else → enqueue paper.process job with Zotero metadata as seed

    Persists last library version in user_config as 'zotero.last_library_version'.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    cfg = await _get_zotero_config(db_pool)

    if not cfg.get("enabled"):
        logger.debug("Zotero poll: integration disabled")
        return {"status": "disabled"}

    if not cfg.get("poll_enabled", False):
        logger.debug("Zotero poll: poll_enabled is false")
        return {"status": "poll_disabled"}

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    if not api_key or not user_id:
        logger.warning("Zotero poll: api_key or user_id not configured")
        return {"status": "disabled"}

    # Read last known library version (persisted as a JSON number in user_config).
    last_version: int = 0
    raw_version = cfg.get("last_library_version")
    if raw_version is not None:
        last_version = int(raw_version)

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),
        http_client=http_client,
    )

    try:
        items, new_version = await client.fetch_items_since(last_version)
    except Exception:
        logger.error("Zotero poll: fetch_items_since failed", exc_info=True)
        return {"status": "error", "message": "fetch failed"}

    new_count = 0
    linked_count = 0
    enqueued_count = 0

    for item in items:
        data: dict[str, Any] = item.get("data", {})
        item_key: str = data.get("key", item.get("key", ""))

        # Skip items that originated in JARVIS.
        extra: str = data.get("extra", "") or ""
        if "jarvis_paper_id=" in extra:
            continue

        new_count += 1
        doi: str = data.get("DOI", "") or ""

        # DOI deduplication — link to existing JARVIS paper if found.
        if doi:
            try:
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT id, zotero_item_key FROM papers WHERE metadata->>'doi' = $1",
                        doi,
                    )
                if row:
                    if not row["zotero_item_key"] and item_key:
                        async with db_pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE papers SET zotero_item_key = $1 WHERE id = $2",
                                item_key,
                                row["id"],
                            )
                    linked_count += 1
                    continue
            except Exception:
                logger.warning("Zotero poll: DOI lookup failed for key %s", item_key, exc_info=True)

        # Build author list from Zotero creators.
        creators: list[dict[str, str]] = data.get("creators", []) or []
        authors: list[str] = []
        for c in creators:
            first = c.get("firstName", "")
            last = c.get("lastName", "")
            name = f"{first} {last}".strip() if first else last
            if name:
                authors.append(name)

        # Upsert paper into DB first so paper_id exists, then enqueue paper.analyze.
        title: str = data.get("title", "") or ""
        abstract: str = data.get("abstractNote", "") or ""
        url: str = data.get("url", "") or ""
        if not url:
            url = f"https://www.zotero.org/items/{item_key}"
        metadata: dict[str, Any] = {"zotero_item_key": item_key}
        if doi:
            metadata["doi"] = doi

        paper_create = PaperCreate(
            external_id=f"zotero:{item_key}",
            source_type=SourceType.LOCAL,
            title=title or f"Zotero item {item_key}",
            authors=authors,
            abstract=abstract or None,
            url=url,
            metadata=metadata,
        )

        try:
            async with db_pool.acquire() as conn:
                row = await upsert_paper(conn, paper_create)
                paper_id: int = row["id"]
                # Store the Zotero item key on the paper row.
                if item_key:
                    await conn.execute(
                        "UPDATE papers SET zotero_item_key = $1"
                        " WHERE id = $2 AND zotero_item_key IS NULL",
                        item_key,
                        paper_id,
                    )
            await jobs_lib.enqueue(db_pool, "paper.analyze", {"paper_id": paper_id})
            enqueued_count += 1
        except Exception:
            logger.error(
                "Zotero poll: failed to upsert/enqueue paper for key %s", item_key, exc_info=True
            )

    # Persist updated library version.
    if new_version != last_version:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_config (key, value)
                    VALUES ('zotero.last_library_version', $1::jsonb)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    new_version,
                )
        except Exception:
            logger.error("Zotero poll: failed to persist last_library_version", exc_info=True)

    logger.info(
        "Zotero poll complete: new=%d linked=%d enqueued=%d version=%d→%d",
        new_count,
        linked_count,
        enqueued_count,
        last_version,
        new_version,
    )
    return {
        "status": "ok",
        "new_items": new_count,
        "linked": linked_count,
        "enqueued": enqueued_count,
        "version_from": last_version,
        "version_to": new_version,
    }


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------


@job_handler("zotero.push")
async def _zotero_push_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for zotero.push — push a single paper to Zotero.

    Payload keys:
        paper_id (int): DB paper ID to push.
    """
    paper_id: int = payload["paper_id"]
    await ctx.update_progress(0.1, "Starting Zotero push")
    await push_paper_to_zotero(paper_id, pool, http_client)
    await ctx.update_progress(1.0, "Done")
    return {"paper_id": paper_id, "status": "pushed"}


@job_handler("zotero.resync")
async def _zotero_resync_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for zotero.resync — force re-push a paper to Zotero.

    Payload keys:
        paper_id (int): DB paper ID to resync.
    """
    paper_id: int = payload["paper_id"]
    await ctx.update_progress(0.1, "Clearing existing Zotero key")
    await resync_paper_to_zotero(paper_id, pool, http_client)
    await ctx.update_progress(1.0, "Done")
    return {"paper_id": paper_id, "status": "resynced"}


@job_handler("zotero.sync_from_zotero")
async def _zotero_sync_from_zotero_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for zotero.sync_from_zotero — incremental library poll.

    Polls the Zotero library for items added since the last known version and
    enqueues paper.process jobs for any new items not originating in JARVIS.
    """
    await ctx.update_progress(0.1, "Starting Zotero library poll")
    result = await poll_zotero_library(pool, http_client)
    await ctx.update_progress(1.0, "Done")
    return result
