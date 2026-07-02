"""Zotero item push/resync: advisory locking and project-collection filing."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from jarvis_common.advisory_lock import _kind_lock_key

from paper_ingestion.integrations._zotero_config import (
    ZoteroConfigDecryptError,
    _get_zotero_config,
    _resolve_zotero_user_id,
)

logger = logging.getLogger("paper_ingestion.integrations.zotero_service")


@asynccontextmanager
async def _session_push_lock(conn: Any, paper_id: int, owner_id: int):
    """Serialize push/resync for a single (paper, owner) via a session advisory lock.

    Session-scoped (not xact-scoped) so the inner item-key UPSERT can keep its
    unwrapped ON CONFLICT / UniqueViolation handling (a transaction-scoped lock
    would force-wrap that body and break the deliberately-autocommit handler).

    Released in ``finally``. Edge case: if the connection died mid-section, the
    ``finally`` ``pg_advisory_unlock`` itself raises — but that is not a leak:
    asyncpg discards a connection that errors on release rather than returning it
    to the pool, and Postgres frees every session-level advisory lock the moment
    that backend session ends, so the lock is never inherited by a future pooled
    borrower. The raised unlock propagates out of the context manager (the push
    has already failed in that case), which is acceptable.
    """
    key = _kind_lock_key(f"zotero.push:{paper_id}:{owner_id}")
    await conn.execute("SELECT pg_advisory_lock($1)", key)
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", key)


async def _resolve_project_collection_keys(
    conn: Any, client: Any, project_ids: list[int], owner_user_id: int | None
) -> list[str]:
    """Resolve (creating + persisting on first use) the Zotero collection key for each
    linked project. Mirrors the create-branch loop; idempotent via ensure_collection."""
    collection_keys: list[str] = []
    for project_id in project_ids:
        try:
            project = await conn.fetchrow(
                """
                SELECT id, name, zotero_collection_key
                FROM projects
                WHERE id = $1
                  AND ($2::bigint IS NULL OR user_id IS NOT DISTINCT FROM $2)
                """,
                project_id,
                owner_user_id,
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
    return collection_keys


async def push_paper_to_zotero(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    owner_user_id: int | None = None,
    force: bool = False,
) -> None:
    """Push a paper to Zotero. Only pushes papers that have at least one project link.

    Push flow:
    1. Load config — skip if Zotero credentials are missing.
    2. Load paper + project links — skip if no projects linked.
    3. Deduplicate by DOI against existing Zotero items.
    4. Create Zotero item with collections per linked project.
    5. Store zotero_item_key + zotero_last_pushed_at in the per-user link table.
    6. Best-effort fetch of Better BibTeX citation key.

    A single DB connection is acquired for the entire operation to avoid
    pool exhaustion under concurrent pushes.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    try:
        cfg = await _get_zotero_config(db_pool, user_id=owner_user_id)
    except ZoteroConfigDecryptError:
        logger.warning(
            "Zotero config decryption failed for paper %d push — "
            "stored credentials are unreadable (key rotation?); "
            "operator must re-save Zotero API key in Settings",
            paper_id,
        )
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
        await _push_paper_with_conn(
            paper_id,
            conn,
            client,
            owner_user_id=owner_user_id,
            force=force,
        )

    logger.info("Paper %d pushed to Zotero", paper_id)


async def _push_paper_with_conn(
    paper_id: int,
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    client: Any,
    *,
    owner_user_id: int | None = None,
    force: bool = False,
) -> None:
    """Internal push implementation that operates on a single DB connection."""
    resolved_owner_id = await _resolve_zotero_user_id(conn, owner_user_id)
    if resolved_owner_id is None:
        # Ambiguous ownership (no explicit owner AND multiple active users): the
        # per-user link row cannot be attributed safely, so skip the push rather
        # than create a Zotero item with nowhere to record its key.
        logger.warning("Zotero push: ambiguous owner for paper %d, skipping", paper_id)
        return

    async with _session_push_lock(conn, paper_id, resolved_owner_id):
        if force:
            # Resync: clear this owner's item_key inside the lock so the already-pushed
            # early-return below is bypassed and no concurrent push can observe a NULL
            # window (it blocks on the lock, then sees the freshly written key).
            await conn.execute(
                "UPDATE paper_user_zotero_links"
                " SET zotero_item_key = NULL, updated_at = NOW()"
                " WHERE paper_id = $1 AND user_id = $2",
                paper_id,
                resolved_owner_id,
            )

        paper = await conn.fetchrow(
            """
            SELECT p.id, p.title, p.authors, p.metadata->>'doi' AS doi, p.url, p.abstract,
                   p.pdf_local_path, l.zotero_item_key,
                   array_agg(DISTINCT owner_project.id)
                       FILTER (WHERE owner_project.id IS NOT NULL) AS project_ids
            FROM papers p
            LEFT JOIN project_papers pp ON pp.paper_id = p.id
            LEFT JOIN projects owner_project
              ON owner_project.id = pp.project_id
             AND ($2::bigint IS NULL OR owner_project.user_id IS NOT DISTINCT FROM $2)
            LEFT JOIN paper_user_zotero_links l ON l.paper_id = p.id AND l.user_id = $3
            WHERE p.id = $1
            GROUP BY p.id, l.zotero_item_key
            """,
            paper_id,
            owner_user_id,
            resolved_owner_id,
        )

        if not paper:
            logger.warning("Zotero push: paper %d not found", paper_id)
            return

        project_ids: list[int] = list(paper["project_ids"] or [])
        if not project_ids:
            logger.info("Zotero push skipped: paper %d has no project links", paper_id)
            return

        # Already pushed for this owner: reconcile collections (resync clears the
        # key first, so a forced re-push falls through to the create branch instead).
        if paper["zotero_item_key"]:
            # A project linked AFTER the first push must still file the existing
            # item into that project's collection rather than no-op.
            collection_keys = await _resolve_project_collection_keys(
                conn, client, project_ids, owner_user_id
            )
            if collection_keys:
                try:
                    await client.add_item_to_collections(paper["zotero_item_key"], collection_keys)
                except Exception:
                    logger.warning(
                        "Zotero collection reconcile failed for paper %d", paper_id, exc_info=True
                    )
            logger.debug(
                "Paper %d already in Zotero (%s); collections reconciled",
                paper_id,
                paper["zotero_item_key"],
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
            collection_keys = await _resolve_project_collection_keys(
                conn, client, project_ids, owner_user_id
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

        # Persist the Zotero item key in the per-user link table (the global
        # papers.zotero_* columns are no longer written — linkage is per-(paper,user)).
        try:
            await conn.execute(
                """
                INSERT INTO paper_user_zotero_links
                    (paper_id, user_id, zotero_item_key, zotero_last_pushed_at, updated_at)
                VALUES ($1, $2, $3, NOW(), NOW())
                ON CONFLICT (paper_id, user_id) DO UPDATE
                   SET zotero_item_key = EXCLUDED.zotero_item_key,
                       zotero_last_pushed_at = EXCLUDED.zotero_last_pushed_at,
                       updated_at = NOW()
                """,
                paper_id,
                resolved_owner_id,
                zotero_key,
            )
        except asyncpg.UniqueViolationError:
            # DOI dedup can resolve a Zotero item this user already linked to a sibling
            # papers row sharing the DOI (papers are deduped by external_id, not DOI).
            # The partial unique index uq_pu_zotero_item(user_id, zotero_item_key) —
            # which ON CONFLICT (paper_id, user_id) does NOT arbitrate — fires. The
            # paper is already represented in the user's Zotero, so treat it as done
            # rather than aborting the (unwrapped) push/resync job.
            logger.info(
                "Zotero push: paper %d maps to item %s already linked for user %s "
                "(shared DOI, sibling paper row); skipping duplicate link",
                paper_id,
                zotero_key,
                resolved_owner_id,
            )
            return

        # Best-effort: fetch Better BibTeX citation key from local BBT plugin.
        try:
            bbt_key = await client.fetch_bbt_citation_key(zotero_key)
            if bbt_key:
                await conn.execute(
                    """
                    INSERT INTO paper_user_zotero_links
                        (paper_id, user_id, zotero_citation_key, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (paper_id, user_id) DO UPDATE
                       SET zotero_citation_key = EXCLUDED.zotero_citation_key,
                           updated_at = NOW()
                    """,
                    paper_id,
                    resolved_owner_id,
                    bbt_key,
                )
        except Exception:
            logger.debug(
                "BBT citation key fetch failed for paper %d (non-fatal)", paper_id, exc_info=True
            )


async def resync_paper_to_zotero(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    owner_user_id: int | None = None,
) -> None:
    """Force re-push a paper to Zotero. The owner's link item_key is cleared inside
    the locked push body (force=True), so the clear→re-push window cannot duplicate
    the Zotero item under a concurrent push."""
    await push_paper_to_zotero(
        paper_id, db_pool, http_client, owner_user_id=owner_user_id, force=True
    )
