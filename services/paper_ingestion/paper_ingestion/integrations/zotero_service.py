"""Zotero push service — business logic and job handlers."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
import httpx
from jarvis_common.jobs import JobContext
from jarvis_common.library import add_to_library
from jarvis_common.paper_state import upsert_paper_user_state as _upsert_paper_user_state
from jarvis_common.task_registry import KIND_TO_TASK

from paper_ingestion.models.papers import PaperCreate, SourceType
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger(__name__)

# Maximum number of items enqueued per sync cycle.  When this limit is hit the
# library version cursor is NOT advanced so the next sync resumes from the same
# point and processes the next batch.
MAX_ENQUEUE_PER_SYNC = 20


async def _get_zotero_config(
    db_pool: asyncpg.Pool,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Read Zotero settings from user_config. Returns dict with short keys.

    Prefers encrypted_value (post-Sprint-1 UI saves) over plaintext value
    (legacy rows written before encryption was introduced).

    If decryption fails (e.g. key rotation, corrupted ciphertext) the whole
    config is treated as missing — callers will hit the "no api_key" branch
    and skip the operation gracefully.
    """
    from jarvis_common.crypto import decrypt_secret

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
               FROM user_config
               WHERE key LIKE 'zotero.%' AND (user_id = $1 OR user_id IS NULL)
               ORDER BY key, user_id IS NULL""",
            user_id,
        )
    config: dict[str, Any] = {}
    for row in rows:
        short_key = row["key"][len("zotero.") :]
        enc = row.get("encrypted_value")
        if enc is not None:
            # Post-Sprint-1 row: decrypt Fernet ciphertext stored as BYTEA.
            try:
                config[short_key] = decrypt_secret(enc.decode("ascii"))
            except Exception:
                logger.warning(
                    "Zotero config decrypt failed for key %r; treating Zotero config as missing",
                    short_key,
                )
                return {"_decrypt_error": True}
        else:
            # Legacy plaintext row (or non-secret scalar).
            # asyncpg JSONB codec auto-decodes objects/arrays/booleans;
            # scalar strings come back as str — no manual json.loads() needed.
            config[short_key] = row["value"]
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

    A single DB connection is acquired for the entire operation to avoid
    pool exhaustion under concurrent pushes (PI-EDGE-013).
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    cfg = await _get_zotero_config(db_pool)
    if not cfg.get("enabled"):
        logger.debug("Zotero disabled, skipping push for paper %d", paper_id)
        return

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
    if not api_key or not user_id:
        logger.warning(
            "Zotero API key or user_id not configured, skipping push for paper %d", paper_id
        )
        return

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),  # type: ignore[arg-type]
        group_id=group_id,
        http_client=http_client,
    )

    async with db_pool.acquire() as conn:
        await _push_paper_with_conn(paper_id, conn, client)

    logger.info("Paper %d pushed to Zotero", paper_id)


async def _push_paper_with_conn(
    paper_id: int,
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    client: Any,
) -> None:
    """Internal push implementation that operates on a single DB connection."""
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
    await conn.execute(
        "UPDATE papers SET zotero_item_key = $1, zotero_last_pushed_at = NOW() WHERE id = $2",
        zotero_key,
        paper_id,
    )

    # Best-effort: fetch Better BibTeX citation key from local BBT plugin.
    try:
        bbt_key = await client.fetch_bbt_citation_key(zotero_key)
        if bbt_key:
            await conn.execute(
                "UPDATE papers SET zotero_citation_key = $1 WHERE id = $2",
                bbt_key,
                paper_id,
            )
    except Exception:
        logger.debug("BBT citation key fetch failed for paper %d (non-fatal)", paper_id)


async def resync_paper_to_zotero(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
) -> None:
    """Force re-push paper to Zotero (clears existing zotero_item_key first)."""
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE papers SET zotero_item_key = NULL WHERE id = $1", paper_id)
    await push_paper_to_zotero(paper_id, db_pool, http_client)


def _annotation_page_number(value: Any) -> int | None:
    """Parse Zotero's free-form page label into a 1-indexed page number."""
    if value is None:
        return None
    try:
        page = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return page if page >= 1 else None


async def sync_annotations_for_paper(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Import Zotero PDF annotations into ``paper_notes`` for a linked paper.

    Imported annotations are stored as read-only notes with ``source='zotero'``
    and idempotently upserted by ``(paper_id, zotero_annotation_key)``. They are
    not copied into verified evidence or knowledge-graph tables.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    cfg = await _get_zotero_config(db_pool)
    if not cfg.get("enabled"):
        return {"paper_id": paper_id, "imported": 0, "status": "disabled"}

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
    if not api_key or not user_id:
        return {"paper_id": paper_id, "imported": 0, "status": "disabled"}

    async with db_pool.acquire() as conn:
        paper = await conn.fetchrow(
            "SELECT id, zotero_item_key, discovered_by FROM papers WHERE id = $1",
            paper_id,
        )
    if not paper:
        return {"paper_id": paper_id, "imported": 0, "status": "not_found"}
    zotero_item_key = paper["zotero_item_key"]
    # Sprint B: attribute imported annotations to the paper's discoverer
    # (audit-trail column). Tolerate fixtures missing the column (NULL = system).
    try:
        paper_owner_user_id = paper["discovered_by"]
    except (KeyError, IndexError):
        paper_owner_user_id = None
    if not zotero_item_key:
        return {"paper_id": paper_id, "imported": 0, "status": "not_linked"}

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),  # type: ignore[arg-type]
        group_id=group_id,
        http_client=http_client,
    )
    annotations = await client.get_item_children(str(zotero_item_key), item_type="annotation")

    imported = 0
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for item in annotations:
                key = item.get("key") or item.get("data", {}).get("key")
                data = item.get("data", {})
                if not key:
                    continue
                highlight = (data.get("annotationText") or "").strip() or None
                comment = (data.get("annotationComment") or "").strip()
                note = comment or highlight
                if not note:
                    continue
                page_number = _annotation_page_number(data.get("annotationPageLabel"))
                await conn.execute(
                    """
                    INSERT INTO paper_notes
                        (paper_id, source, zotero_annotation_key, user_note, highlight_text,
                         page_number, user_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (paper_id, zotero_annotation_key) DO UPDATE
                        SET user_note      = EXCLUDED.user_note,
                            highlight_text = EXCLUDED.highlight_text,
                            page_number    = EXCLUDED.page_number,
                            verification_status =
                                CASE
                                    WHEN paper_notes.highlight_text
                                         IS DISTINCT FROM EXCLUDED.highlight_text
                                    THEN 'unverified'
                                    ELSE paper_notes.verification_status
                                END,
                            verified_quote =
                                CASE
                                    WHEN paper_notes.highlight_text
                                         IS DISTINCT FROM EXCLUDED.highlight_text
                                    THEN NULL
                                    ELSE paper_notes.verified_quote
                                END,
                            verified_page_number =
                                CASE
                                    WHEN paper_notes.highlight_text
                                         IS DISTINCT FROM EXCLUDED.highlight_text
                                    THEN NULL
                                    ELSE paper_notes.verified_page_number
                                END,
                            promoted_at =
                                CASE
                                    WHEN paper_notes.highlight_text
                                         IS DISTINCT FROM EXCLUDED.highlight_text
                                    THEN NULL
                                    ELSE paper_notes.promoted_at
                                END
                    """,
                    paper_id,
                    "zotero",
                    str(key),
                    note,
                    highlight,
                    page_number,
                    paper_owner_user_id,
                )
                imported += 1

    return {"paper_id": paper_id, "imported": imported, "status": "ok"}


async def poll_zotero_library(
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    polling_user_id: int | None = None,
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
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
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
        library_type=str(library_type),  # type: ignore[arg-type]
        group_id=group_id,
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
    capped = False  # True when we hit MAX_ENQUEUE_PER_SYNC mid-batch.
    failed_keys: list[str] = []

    for item in items:
        if enqueued_count >= MAX_ENQUEUE_PER_SYNC:
            capped = True
            break
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
                        "SELECT id, zotero_item_key, discovered_by FROM papers"
                        " WHERE metadata->>'doi' = $1",
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
                        try:
                            await KIND_TO_TASK["zotero.sync_annotations"].defer_async(
                                job_id=str(uuid.uuid4()),
                                # Sprint B: attribute to paper's discoverer
                                # (audit-trail column).
                                user_id=row["discovered_by"],
                                paper_id=row["id"],
                            )
                        except Exception:
                            logger.debug(
                                "Zotero poll: failed to enqueue annotation sync for %s",
                                row["id"],
                                exc_info=True,
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
            discovery_origin="user_initiated",
        )

        try:
            async with db_pool.acquire() as conn:
                # Sprint B canonical-corpus: insert canonical, then mirror
                # into the polling user's library so the imported item
                # appears in *their* feed. ``discovered_by`` keeps the audit
                # trail.
                row = await upsert_paper(conn, paper_create, discovered_by=polling_user_id)
                paper_id = row["id"]
                if polling_user_id is not None:
                    await add_to_library(
                        conn,
                        user_id=polling_user_id,
                        paper_id=paper_id,
                        added_via="zotero_pull",
                    )
                # First-sync wins: INSERT to_read state but never overwrite
                # existing user state (user may have trashed the paper).
                await _upsert_paper_user_state(
                    conn,
                    paper_id,
                    polling_user_id,
                    state="to_read",
                    starred=False,
                    on_conflict="do_nothing",
                )
                # Store the Zotero item key on the paper row.
                if item_key:
                    await conn.execute(
                        "UPDATE papers SET zotero_item_key = $1"
                        " WHERE id = $2 AND zotero_item_key IS NULL",
                        item_key,
                        paper_id,
                    )
            await KIND_TO_TASK["paper.analyze"].defer_async(
                job_id=str(uuid.uuid4()),
                user_id=polling_user_id,
                paper_id=paper_id,
            )
            enqueued_count += 1
        except Exception:
            logger.error(
                "Zotero poll: failed to upsert/enqueue paper for key %s", item_key, exc_info=True
            )
            failed_keys.append(item_key)

    # If any items failed, log a summary error and pin the cursor so the next
    # poll retries the entire batch from the same starting version.
    if failed_keys:
        logger.error(
            "Zotero poll: %d items failed; first 5: %s",
            len(failed_keys),
            failed_keys[:5],
        )
        new_version = last_version

    # Persist updated library version.
    # If the enqueue cap was hit, do NOT advance the cursor — the next sync
    # will re-fetch items starting from last_version and process the next batch.
    if capped:
        new_version = last_version
        logger.info(
            "Zotero poll: enqueue cap (%d) reached — deferring version advance to next sync",
            MAX_ENQUEUE_PER_SYNC,
        )
    if new_version != last_version:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_config (user_id, key, value)
                    VALUES ($2, 'zotero.last_library_version', $1::jsonb)
                    ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    new_version,
                    polling_user_id,
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


async def _zotero_push_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for zotero.push — push a single paper to Zotero.

    Payload keys:
        paper_id (int): DB paper ID to push.
        user_id (int | None): Caller user ID for ownership check.
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    paper_id: int = payload["paper_id"]
    user_id = payload.get("user_id")

    # Re-validate ownership at job execution time to prevent IDOR via queued jobs.
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    await ctx.update_progress(0.1, "Starting Zotero push")
    await push_paper_to_zotero(paper_id, pool, http_client)
    await ctx.update_progress(1.0, "Done")
    return {"paper_id": paper_id, "status": "pushed"}


async def _zotero_resync_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for zotero.resync — force re-push a paper to Zotero.

    Payload keys:
        paper_id (int): DB paper ID to resync.
        user_id (int | None): Caller user ID for ownership check.
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    paper_id: int = payload["paper_id"]
    user_id = payload.get("user_id")

    # Re-validate ownership at job execution time to prevent IDOR via queued jobs.
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    await ctx.update_progress(0.1, "Clearing existing Zotero key")
    await resync_paper_to_zotero(paper_id, pool, http_client)
    await ctx.update_progress(1.0, "Done")
    return {"paper_id": paper_id, "status": "resynced"}


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
    # WS-2D: thread caller user_id through so imported papers/state/annotations
    # are attributed correctly. NULL when scheduler-cron-invoked (system poll).
    polling_user_id = payload.get("user_id")
    result = await poll_zotero_library(pool, http_client, polling_user_id=polling_user_id)
    await ctx.update_progress(1.0, "Done")
    return result


async def _zotero_sync_annotations_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Job handler for importing Zotero annotations for a linked paper.

    Payload keys:
        paper_id (int): DB paper ID to sync annotations for.
        user_id (int | None): Caller user ID for ownership check.
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    paper_id = int(payload["paper_id"])
    user_id = payload.get("user_id")

    # Re-validate ownership at job execution time to prevent IDOR via queued jobs.
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    await ctx.update_progress(0.1, "Fetching Zotero annotations")
    result = await sync_annotations_for_paper(paper_id, pool, http_client)
    await ctx.update_progress(1.0, "Done")
    return result
