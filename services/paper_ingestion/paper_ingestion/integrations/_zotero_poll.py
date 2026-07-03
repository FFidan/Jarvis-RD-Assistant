"""Zotero library polling: item parsing, DOI linking, ingestion, cursor."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg
import httpx
from jarvis_common.library import add_to_library
from jarvis_common.paper_state import upsert_paper_user_state as _upsert_paper_user_state
from jarvis_common.task_registry import KIND_TO_TASK
from pydantic import ValidationError

from paper_ingestion.integrations._zotero_config import (
    ZoteroConfigDecryptError,
    _get_zotero_config,
    _resolve_zotero_user_id,
)
from paper_ingestion.models.papers import PaperCreate, SourceType
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger("paper_ingestion.integrations.zotero_service")

# Maximum number of items enqueued per sync cycle.  When this limit is hit the
# library version cursor is NOT advanced so the next sync resumes from the same
# point and processes the next batch.
MAX_ENQUEUE_PER_SYNC = 20


@dataclass(frozen=True, slots=True)
class _PollConfig:
    """Resolved, decrypted Zotero polling config for one cycle."""

    api_key: str
    user_id: str
    library_type: str
    group_id: int | None
    last_version: int


async def _load_poll_config(
    db_pool: asyncpg.Pool, polling_user_id: int | None
) -> _PollConfig | dict[str, str]:
    """Load + validate the Zotero poll config.

    Returns a typed ``_PollConfig`` on success, or an early-status dict
    (``config_decrypt_failed`` / ``disabled`` / ``poll_disabled``) that the
    caller returns verbatim.
    """
    try:
        cfg = await _get_zotero_config(db_pool, user_id=polling_user_id)
    except ZoteroConfigDecryptError:
        logger.warning(
            "Zotero poll: config decryption failed — stored credentials are unreadable "
            "(key rotation?); operator must re-save Zotero API key in Settings"
        )
        return {"status": "config_decrypt_failed"}

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
    if not api_key or not user_id:
        logger.warning("Zotero poll: api_key or user_id not configured")
        return {"status": "disabled"}

    if not cfg.get("poll_enabled", False):
        logger.debug("Zotero poll: poll_enabled is false")
        return {"status": "poll_disabled"}

    # Read last known library version (persisted as a JSON number in user_config).
    last_version: int = 0
    raw_version = cfg.get("last_library_version")
    if raw_version is not None:
        last_version = int(raw_version)

    return _PollConfig(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),
        group_id=group_id,
        last_version=last_version,
    )


@dataclass(frozen=True, slots=True)
class _ParsedZoteroItem:
    """Pure projection of a single Zotero API item into ingestion inputs."""

    item_key: str
    doi: str
    authors: list[str]
    url: str
    metadata: dict[str, Any]
    paper_create: PaperCreate


def _parse_zotero_item(
    data: dict[str, Any], outer_item: dict[str, Any]
) -> _ParsedZoteroItem | None:
    """Project a Zotero item into ingestion inputs (pure, no I/O).

    ``data`` is the nested ``item["data"]`` dict; ``outer_item`` is the
    top-level Zotero API response so the ``outer_item.get("key", "")`` key
    fallback is preserved. Returns ``None`` for items that originated in
    JARVIS (Extra contains ``jarvis_paper_id=``).
    """
    item_key: str = data.get("key", outer_item.get("key", ""))

    # Skip items that originated in JARVIS.
    extra: str = data.get("extra", "") or ""
    if "jarvis_paper_id=" in extra:
        return None

    doi: str = data.get("DOI", "") or ""

    # Build author list from Zotero creators.
    creators: list[dict[str, str]] = data.get("creators", []) or []
    authors: list[str] = []
    for c in creators:
        first = c.get("firstName", "")
        last = c.get("lastName", "")
        name = f"{first} {last}".strip() if first else last
        if name:
            authors.append(name)

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
        source_type=SourceType.ZOTERO,
        title=title or f"Zotero item {item_key}",
        authors=authors,
        abstract=abstract or None,
        url=url,
        metadata=metadata,
        discovery_origin="user_initiated",
    )

    return _ParsedZoteroItem(
        item_key=item_key,
        doi=doi,
        authors=authors,
        url=url,
        metadata=metadata,
        paper_create=paper_create,
    )


def _safe_parse_zotero_item(
    data: dict[str, Any], outer_item: dict[str, Any], item_key: str
) -> _ParsedZoteroItem | None:
    """Call _parse_zotero_item, returning None (and logging) on validation failure.

    Isolates the try/except so the per-item exception branch does not grow
    poll_zotero_library's branch count (PLR0912).
    """
    try:
        return _parse_zotero_item(data, outer_item)
    except (ValidationError, ValueError):
        logger.warning(
            "Zotero poll: skipping malformed item %s — parse failed",
            item_key,
            exc_info=True,
        )
        return None


async def _link_existing_by_doi(
    db_pool: asyncpg.Pool, doi: str, item_key: str, polling_user_id: int | None
) -> Literal["linked"] | None:
    """DOI deduplication — link to an existing JARVIS paper if one matches.

    Takes the raw ``doi``/``item_key`` (not a parsed item) so the caller can
    resolve the link BEFORE projecting the Zotero item into a PaperCreate model:
    a linked item must never be validated, since a malformed url/over-long title
    on an item that simply matches a paper already in the library would otherwise
    raise and abort the whole poll.

    Returns ``"linked"`` when an existing paper was found and linked (the
    caller skips ingestion); ``None`` when no match was found or the lookup
    failed (the caller falls through to ingest a new paper).
    """
    try:
        async with db_pool.acquire() as conn:
            resolved_polling_user_id = await _resolve_zotero_user_id(conn, polling_user_id)
            row = await conn.fetchrow(
                "SELECT p.id, l.zotero_item_key, p.discovered_by FROM papers p"
                " LEFT JOIN paper_user_zotero_links l"
                "   ON l.paper_id = p.id AND l.user_id = $2"
                " WHERE p.metadata->>'doi' = $1",
                doi,
                resolved_polling_user_id,
            )
        if row:
            if resolved_polling_user_id is not None and not row["zotero_item_key"] and item_key:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO paper_user_zotero_links
                            (paper_id, user_id, zotero_item_key, updated_at)
                        VALUES ($1, $2, $3, NOW())
                        ON CONFLICT (paper_id, user_id) DO UPDATE
                           SET zotero_item_key = EXCLUDED.zotero_item_key,
                               updated_at = NOW()
                        """,
                        row["id"],
                        resolved_polling_user_id,
                        item_key,
                    )
                try:
                    await KIND_TO_TASK["zotero.sync_annotations"].defer_async(
                        job_id=str(uuid.uuid4()),
                        # Attribute to the syncing user (who triggered this poll).
                        user_id=resolved_polling_user_id,
                        paper_id=row["id"],
                    )
                except Exception:
                    logger.debug(
                        "Zotero poll: failed to enqueue annotation sync for %s",
                        row["id"],
                        exc_info=True,
                    )
            if polling_user_id is not None:
                async with db_pool.acquire() as conn:
                    await add_to_library(
                        conn,
                        user_id=polling_user_id,
                        paper_id=row["id"],
                        added_via="zotero_pull",
                    )
                    # First-sync wins: never overwrite existing user
                    # state (the user may have trashed the paper).
                    await _upsert_paper_user_state(
                        conn,
                        row["id"],
                        polling_user_id,
                        state="to_read",
                        starred=False,
                        on_conflict="do_nothing",
                    )
            return "linked"
    except Exception:
        logger.warning("Zotero poll: DOI lookup failed for key %s", item_key, exc_info=True)
    return None


async def _ingest_new_item(
    db_pool: asyncpg.Pool,
    paper_create: PaperCreate,
    item_key: str,
    polling_user_id: int | None,
) -> bool:
    """Upsert a new paper, mirror it into the polling user's library, store the
    Zotero link, and enqueue ``paper.analyze`` for brand-new papers.

    Returns ``True`` when ``paper.analyze`` was enqueued (the paper was an
    insert), ``False`` otherwise. Raises on DB/enqueue failure so the caller
    can pin the cursor.
    """
    async with db_pool.acquire() as conn:
        # Insert canonical, then mirror into the polling user's library
        # so the imported item appears in *their* feed.
        # ``discovered_by`` keeps the audit trail.
        row = await upsert_paper(conn, paper_create, discovered_by=polling_user_id)
        paper_id = row["id"]
        is_new_paper = bool(row["is_insert"])
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
        # Store the Zotero item key in the polling user's link row
        # (at-most-once: only when the link's item_key is still NULL).
        if item_key:
            resolved_polling_user_id = await _resolve_zotero_user_id(conn, polling_user_id)
            if resolved_polling_user_id is not None:
                await conn.execute(
                    """
                    INSERT INTO paper_user_zotero_links
                        (paper_id, user_id, zotero_item_key, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (paper_id, user_id) DO UPDATE
                       SET zotero_item_key = EXCLUDED.zotero_item_key,
                           updated_at = NOW()
                     WHERE paper_user_zotero_links.zotero_item_key IS NULL
                    """,
                    paper_id,
                    resolved_polling_user_id,
                    item_key,
                )
    # Enqueue gate = is_insert (brand-new paper), NOT an analysis-
    # completion marker. Zotero-imported papers carry no pdf_url, so
    # _paper_analyze_job raises before the download that would flip
    # pdf_downloaded — gating on pdf_downloaded (or any "analyzed?"
    # field) would re-enqueue every already-imported item on each
    # capped/failed re-poll and pin the cursor (storm). is_insert is
    # False on every re-poll, so the cursor advances to the next batch.
    if is_new_paper:
        await KIND_TO_TASK["paper.analyze"].defer_async(
            job_id=str(uuid.uuid4()),
            user_id=polling_user_id,
            paper_id=paper_id,
        )
        return True
    return False


async def _persist_poll_cursor(
    db_pool: asyncpg.Pool, polling_user_id: int | None, new_version: int
) -> None:
    """Persist the updated last-library-version cursor."""
    # polling_user_id may be None (single-tenant / system poll). The
    # user_config unique index is NULLS NOT DISTINCT, so the NULL-user row
    # upserts correctly — persist the cursor instead of skipping (skipping
    # left the cursor at 0 and re-polled the whole library forever).
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


@dataclass(frozen=True, slots=True)
class _PollBatch:
    """Per-cycle counters produced by processing a fetched batch of Zotero items."""

    new_count: int
    linked_count: int
    enqueued_count: int
    capped: bool
    failed_keys: list[str]


async def _process_poll_batch(
    db_pool: asyncpg.Pool,
    items: list[dict[str, Any]],
    polling_user_id: int | None,
) -> _PollBatch:
    """Link or ingest each new item, stopping at the per-cycle enqueue cap."""
    new_count = 0
    linked_count = 0
    enqueued_count = 0
    capped = False  # True when we hit MAX_ENQUEUE_PER_SYNC mid-batch.
    failed_keys: list[str] = []

    for outer_item in items:
        if enqueued_count >= MAX_ENQUEUE_PER_SYNC:
            capped = True
            break
        data: dict[str, Any] = outer_item.get("data", {})
        item_key: str = data.get("key", outer_item.get("key", ""))

        # Skip items that originated in JARVIS.
        if "jarvis_paper_id=" in (data.get("extra", "") or ""):
            continue

        new_count += 1

        # DOI deduplication — resolve the link BEFORE projecting the item into a
        # PaperCreate model, so a linked item is never validated: a malformed
        # url/over-long title on an item that simply matches a paper already in
        # the library must not raise here and abort the whole poll.
        doi: str = data.get("DOI", "") or ""
        if doi and await _link_existing_by_doi(db_pool, doi, item_key, polling_user_id) == "linked":
            linked_count += 1
            continue

        # Not linked → project into ingestion inputs.  Malformed items return
        # None from the safe helper (parse failure logged there).
        parsed = _safe_parse_zotero_item(data, outer_item, item_key)
        if parsed is None:
            failed_keys.append(item_key)
            continue

        try:
            if await _ingest_new_item(
                db_pool, parsed.paper_create, parsed.item_key, polling_user_id
            ):
                enqueued_count += 1
        except Exception:
            logger.error(
                "Zotero poll: failed to upsert/enqueue paper for key %s",
                parsed.item_key,
                exc_info=True,
            )
            failed_keys.append(parsed.item_key)

    return _PollBatch(new_count, linked_count, enqueued_count, capped, failed_keys)


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

    config = await _load_poll_config(db_pool, polling_user_id)
    if isinstance(config, dict):
        return config
    last_version = config.last_version

    client = ZoteroClient(
        api_key=config.api_key,
        user_id=config.user_id,
        library_type=config.library_type,  # type: ignore[arg-type]
        group_id=config.group_id,
        http_client=http_client,
    )

    try:
        items, new_version = await client.fetch_items_since(last_version)
    except Exception:
        logger.error("Zotero poll: fetch_items_since failed", exc_info=True)
        return {"status": "error", "message": "fetch failed"}

    batch = await _process_poll_batch(db_pool, items, polling_user_id)

    # If any items failed, log a summary error and pin the cursor so the next
    # poll retries the entire batch from the same starting version.
    if batch.failed_keys:
        logger.error(
            "Zotero poll: %d items failed; first 5: %s",
            len(batch.failed_keys),
            batch.failed_keys[:5],
        )
        new_version = last_version

    # Persist updated library version.
    # If the enqueue cap was hit, do NOT advance the cursor — the next sync
    # will re-fetch items starting from last_version and process the next batch.
    if batch.capped:
        new_version = last_version
        logger.info(
            "Zotero poll: enqueue cap (%d) reached — deferring version advance to next sync",
            MAX_ENQUEUE_PER_SYNC,
        )
    if new_version != last_version:
        await _persist_poll_cursor(db_pool, polling_user_id, new_version)

    logger.info(
        "Zotero poll complete: new=%d linked=%d enqueued=%d version=%d→%d",
        batch.new_count,
        batch.linked_count,
        batch.enqueued_count,
        last_version,
        new_version,
    )
    return {
        "status": "ok",
        "new_items": batch.new_count,
        "linked": batch.linked_count,
        "enqueued": batch.enqueued_count,
        "version_from": last_version,
        "version_to": new_version,
    }
