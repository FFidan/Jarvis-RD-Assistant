"""Zotero item push/resync: advisory locking and project-collection filing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
from jarvis_common.service_auth import ServiceCommand, authorize_service_command

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.integrations._zotero_config import (
    ZoteroConfigDecryptError,
    _get_zotero_config,
    _resolve_zotero_user_id,
)

logger = logging.getLogger("paper_ingestion.integrations.zotero_service")
_PUSH_LEASE_RENEW_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ZoteroItemRef:
    """The local paper and remote Zotero key reconciled as one identity."""

    paper_id: int
    zotero_key: str


async def _claim_push(db_pool: asyncpg.Pool, paper_id: int, user_id: int) -> uuid.UUID | None:
    """Claim remote creation briefly; an expired owner is safely replaced."""
    lease_id = uuid.uuid4()
    async with db_pool.acquire() as conn:
        claimed = await conn.fetchval(
            """INSERT INTO zotero_push_claims (paper_id, user_id, lease_id, lease_expires_at)
               VALUES ($1, $2, $3, NOW() + INTERVAL '5 minutes')
               ON CONFLICT (paper_id, user_id) DO UPDATE
                  SET lease_id = EXCLUDED.lease_id, lease_expires_at = EXCLUDED.lease_expires_at
                WHERE zotero_push_claims.lease_expires_at <= NOW()
               RETURNING lease_id""",
            paper_id,
            user_id,
            lease_id,
        )
    return lease_id if claimed == lease_id else None


async def _release_push_claim(
    db_pool: asyncpg.Pool, paper_id: int, user_id: int, lease_id: uuid.UUID
) -> None:
    """Release only this claimant's lease after remote work is complete."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM zotero_push_claims WHERE paper_id = $1 AND user_id = $2 AND lease_id = $3",
            paper_id,
            user_id,
            lease_id,
        )


async def _maintain_push_claim(
    db_pool: asyncpg.Pool, paper_id: int, user_id: int, lease_id: uuid.UUID
) -> None:
    """Renew a live remote-creation claim or fail before another worker can take it."""
    while True:
        await asyncio.sleep(_PUSH_LEASE_RENEW_SECONDS)
        async with db_pool.acquire() as conn:
            status = await conn.execute(
                """UPDATE zotero_push_claims
                   SET lease_expires_at = NOW() + INTERVAL '5 minutes'
                   WHERE paper_id = $1 AND user_id = $2 AND lease_id = $3""",
                paper_id,
                user_id,
                lease_id,
            )
        if status != "UPDATE 1":
            raise RuntimeError("Zotero push claim was lost during remote creation")


async def _run_with_push_claim(
    operation: Coroutine[Any, Any, Any],
    *,
    db_pool: asyncpg.Pool,
    paper_id: int,
    user_id: int,
    lease_id: uuid.UUID,
) -> Any:
    """Run remote creation while a separately acquired database lease stays live."""
    work = asyncio.create_task(operation)
    heartbeat = asyncio.create_task(_maintain_push_claim(db_pool, paper_id, user_id, lease_id))
    done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
    if heartbeat in done:
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work
        await heartbeat
        raise RuntimeError("Zotero push claim ended unexpectedly")
    heartbeat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat
    return await work


async def _resolve_project_collection_keys(
    db_pool: asyncpg.Pool,
    client: Any,
    project_ids: list[int],
    owner_user_id: int | None,
) -> list[str]:
    """Resolve collection keys without retaining a DB connection across HTTP."""
    collection_keys: list[str] = []
    for project_id in project_ids:
        async with db_pool.acquire() as conn:
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
            raise RuntimeError(
                f"Linked project {project_id} is unavailable for Zotero collection filing"
            )
        if project["zotero_collection_key"]:
            col_key = project["zotero_collection_key"]
        else:
            col_key = await client.ensure_collection(project["name"])
            if owner_user_id is None:
                raise RuntimeError("Zotero collection owner is unavailable")
            await _persist_project_collection_key(
                client,
                project_id=project_id,
                user_id=owner_user_id,
                collection_key=col_key,
            )
        collection_keys.append(col_key)
    return collection_keys


async def _persist_project_collection_key(
    client: Any,
    *,
    project_id: int,
    user_id: int,
    collection_key: str,
) -> None:
    """Persist a collection key only through Learning's signed owner command."""
    settings = get_paper_ingestion_settings()
    token = settings.research_service_token_file.read_text(encoding="utf-8").strip()
    path = f"/internal/domains/projects/{project_id}/zotero-collection"
    headers = await authorize_service_command(
        client,
        platform_url=settings.platform_api_url,
        principal="research",
        token=token,
        command=ServiceCommand(
            audience="learning",
            method="PUT",
            path=path,
            user_id=user_id,
        ),
    )
    response = await client.put(
        f"{settings.learning_engine_url.rstrip('/')}{path}",
        headers=headers,
        json={
            "request_id": str(uuid.UUID(headers["X-Request-Id"])),
            "user_id": user_id,
            "zotero_collection_key": collection_key,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("acknowledged") is not True:
        raise RuntimeError("Learning did not acknowledge Zotero collection metadata")


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

    Database reads and writes use short phases around remote calls. A durable,
    renewable claim serializes remote item creation across workers without
    holding a database connection during Zotero, Learning, or BBT requests.
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

    await _push_paper_with_conn(
        paper_id,
        db_pool,
        client,
        owner_user_id=owner_user_id,
        force=force,
    )

    logger.info("Paper %d pushed to Zotero", paper_id)


def _build_creators(authors: list[Any]) -> list[dict[str, str]]:
    """Map a paper's authors (strings or dicts) to Zotero creator entries."""
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
    return creators


async def _reconcile_existing_item(
    db_pool: asyncpg.Pool,
    client: Any,
    item: ZoteroItemRef,
    project_ids: list[int],
    owner_user_id: int | None,
) -> None:
    """File an already-pushed Zotero item into any newly linked project collections."""
    collection_keys = await _resolve_project_collection_keys(
        db_pool, client, project_ids, owner_user_id
    )
    if collection_keys:
        await client.add_item_to_collections(item.zotero_key, collection_keys)
    logger.debug(
        "Paper %d already in Zotero (%s); collections reconciled",
        item.paper_id,
        item.zotero_key,
    )


async def _lookup_existing_by_doi(client: Any, paper: Any) -> str | None:
    """Return an existing Zotero item key matched by the paper's DOI, or None."""
    if not paper["doi"]:
        return None
    existing_item = await client.search_by_doi(paper["doi"])
    if existing_item:
        logger.info(
            "Paper %d already in Zotero by DOI, reusing key %s",
            paper["id"],
            existing_item["key"],
        )
        return existing_item["key"]
    return None


async def _create_zotero_item(
    db_pool: asyncpg.Pool,
    client: Any,
    paper: Any,
    project_ids: list[int],
    owner_user_id: int | None,
) -> str | None:
    """Create a new Zotero item for the paper; return its key, or None on failure."""
    paper_id = paper["id"]
    creators = _build_creators(paper["authors"] or [])

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
    collection_keys = await _resolve_project_collection_keys(
        db_pool, client, project_ids, owner_user_id
    )
    item_data["collections"] = collection_keys

    try:
        result = await client.create_item(item_data)
        zotero_key = result.get("successful", {}).get("0", {}).get("key")
        if not zotero_key:
            logger.error("Zotero push failed for paper %d: no key in response %s", paper_id, result)
            return None
    except Exception:
        logger.error("Zotero push failed for paper %d", paper_id, exc_info=True)
        raise
    return zotero_key


async def _persist_zotero_link(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    paper_id: int,
    resolved_owner_id: int | None,
    zotero_key: str,
) -> bool:
    """Persist the Zotero item key; False if already linked (shared-DOI sibling = done)."""
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
        return False
    return True


async def _persist_bbt_citation_key(
    db_pool: asyncpg.Pool,
    client: Any,
    paper_id: int,
    resolved_owner_id: int | None,
    zotero_key: str,
) -> None:
    """Fetch BBT remotely, then persist it in a separate database phase."""
    try:
        bbt_key = await client.fetch_bbt_citation_key(zotero_key)
        if bbt_key:
            async with db_pool.acquire() as conn:
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


async def _clear_forced_link(
    db_pool: asyncpg.Pool,
    paper_id: int,
    user_id: int,
    *,
    force: bool,
) -> None:
    """Clear only the selected owner's item key before an explicit resync."""
    if not force:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE paper_user_zotero_links"
            " SET zotero_item_key = NULL, updated_at = NOW()"
            " WHERE paper_id = $1 AND user_id = $2",
            paper_id,
            user_id,
        )


async def _push_paper_with_conn(
    paper_id: int,
    db_pool: asyncpg.Pool,
    client: Any,
    *,
    owner_user_id: int | None = None,
    force: bool = False,
) -> None:
    """Push through short database phases separated from every HTTP call."""
    async with db_pool.acquire() as conn:
        resolved_owner_id = await _resolve_zotero_user_id(conn, owner_user_id)
    if resolved_owner_id is None:
        # Ambiguous ownership (no explicit owner AND multiple active users): the
        # per-user link row cannot be attributed safely, so skip the push rather
        # than create a Zotero item with nowhere to record its key.
        logger.warning("Zotero push: ambiguous owner for paper %d, skipping", paper_id)
        return

    await _clear_forced_link(
        db_pool,
        paper_id,
        resolved_owner_id,
        force=force,
    )

    async with db_pool.acquire() as conn:
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

    if paper["zotero_item_key"]:
        await _reconcile_existing_item(
            db_pool,
            client,
            ZoteroItemRef(paper_id, paper["zotero_item_key"]),
            project_ids=project_ids,
            owner_user_id=resolved_owner_id,
        )
        return

    lease_id = await _claim_push(db_pool, paper_id, resolved_owner_id)
    if lease_id is None:
        logger.info("Zotero push: paper %d already has an active remote-creation claim", paper_id)
        return
    try:

        async def _perform_claimed_push() -> None:
            zotero_key = await _lookup_existing_by_doi(client, paper)
            if zotero_key is None:
                zotero_key = await _create_zotero_item(
                    db_pool, client, paper, project_ids, resolved_owner_id
                )
                if zotero_key is None:
                    return
            else:
                await _reconcile_existing_item(
                    db_pool,
                    client,
                    ZoteroItemRef(paper_id, zotero_key),
                    project_ids=project_ids,
                    owner_user_id=resolved_owner_id,
                )

            async with db_pool.acquire() as conn:
                if not await _persist_zotero_link(conn, paper_id, resolved_owner_id, zotero_key):
                    return
            await _persist_bbt_citation_key(
                db_pool, client, paper_id, resolved_owner_id, zotero_key
            )

        await _run_with_push_claim(
            _perform_claimed_push(),
            db_pool=db_pool,
            paper_id=paper_id,
            user_id=resolved_owner_id,
            lease_id=lease_id,
        )
    finally:
        await _release_push_claim(db_pool, paper_id, resolved_owner_id, lease_id)


async def resync_paper_to_zotero(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    owner_user_id: int | None = None,
) -> None:
    """Force re-push a paper through the same connection-free HTTP phases."""
    await push_paper_to_zotero(
        paper_id, db_pool, http_client, owner_user_id=owner_user_id, force=True
    )
