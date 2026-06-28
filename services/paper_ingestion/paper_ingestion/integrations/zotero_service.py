"""Zotero push service — business logic and job handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import asyncpg
import httpx
from jarvis_common.advisory_lock import _kind_lock_key
from jarvis_common.jobs import ProgressContext
from jarvis_common.library import add_to_library
from jarvis_common.paper_state import upsert_paper_user_state as _upsert_paper_user_state
from jarvis_common.task_registry import KIND_TO_TASK
from pydantic import ValidationError

from paper_ingestion.integrations.zotero_geometry import (
    build_sort_index,
    denormalize_rect_to_zotero,
)
from paper_ingestion.models.papers import PaperCreate, SourceType
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger(__name__)

# Maximum number of items enqueued per sync cycle.  When this limit is hit the
# library version cursor is NOT advanced so the next sync resumes from the same
# point and processes the next batch.
MAX_ENQUEUE_PER_SYNC = 20


class ZoteroConfigDecryptError(Exception):
    """Raised when stored Zotero config cannot be Fernet-decrypted."""


# Keys whose decrypt failure should abort config loading entirely.
# Non-listed encrypted keys are logged and skipped so callers still receive
# the partial config (e.g. last_library_version read failures are non-fatal).
_CRITICAL_ZOTERO_CONFIG_KEYS: frozenset[str] = frozenset({"api_key"})


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
    from jarvis_common.crypto import resolve_secret_row

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
               FROM user_config
               WHERE key LIKE 'zotero.%' AND (user_id = $1 OR user_id IS NULL)
               ORDER BY key, user_id IS NULL""",
            user_id,
        )
    config: dict[str, Any] = {}
    failed_critical: list[str] = []
    failed_non_critical: list[str] = []
    for row in rows:
        short_key = row["key"][len("zotero.") :]
        enc = row.get("encrypted_value")
        if enc is not None:
            # Post-Sprint-1 row: decrypt Fernet ciphertext stored as BYTEA.
            # resolve_secret_row handles memoryview/bytes/str BYTEA variants.
            try:
                config[short_key] = resolve_secret_row({"encrypted_value": enc, "value": None})
            except Exception:
                if short_key in _CRITICAL_ZOTERO_CONFIG_KEYS:
                    logger.warning(
                        "Zotero config decrypt failed for critical key %r; "
                        "operator must re-save Zotero API key in Settings",
                        short_key,
                        exc_info=True,
                    )
                    failed_critical.append(short_key)
                else:
                    logger.warning(
                        "Zotero config decrypt failed for non-critical key %r; skipping",
                        short_key,
                        exc_info=True,
                    )
                    failed_non_critical.append(short_key)
        else:
            # Legacy plaintext row (or non-secret scalar).
            # asyncpg JSONB codec auto-decodes objects/arrays/booleans;
            # scalar strings come back as str — no manual json.loads() needed.
            config[short_key] = row["value"]
    if failed_critical:
        raise ZoteroConfigDecryptError(failed_critical[0])
    return config


async def _resolve_zotero_user_id(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    user_id: int | None,
) -> int | None:
    """Resolve the per-user owner of a ``paper_user_zotero_links`` row.

    The link table needs a concrete ``user_id``, but single-user deployments
    store Zotero config under ``user_config.user_id IS NULL`` and pass
    ``owner_user_id=None`` / ``polling_user_id=None`` through the job boundary.
    Map None -> the sole active user (mirrors migration 0101's sole-user backfill
    arm). Return None when ownership is genuinely ambiguous (None AND more than
    one active user) so callers fail safe — treat it exactly as "not linked" /
    skip the push — rather than misattribute one user's Zotero keys to another.
    """
    if user_id is not None:
        return user_id
    rows = await conn.fetch("SELECT id FROM users WHERE deleted_at IS NULL")
    return rows[0]["id"] if len(rows) == 1 else None


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


# ---------------------------------------------------------------------------
# Highlight export — in-app spatial highlight → openable Zotero annotation
# ---------------------------------------------------------------------------


class _AttachmentUnavailableError(Exception):
    """Internal: the paper's PDF attachment could not be ensured.

    Carries a ``status`` string (``pdf_unavailable`` / ``quota_exceeded`` /
    ``attachment_failed``) that the export functions surface in their result
    dict instead of raising out of the public API.
    """

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _paper_pdf_path(paper_id: int) -> Path:
    """Canonical on-disk PDF path for a paper (``PDF_STORAGE_PATH/{id}.pdf``).

    Reads the live module-level ``PDF_STORAGE_PATH`` at call time so tests can
    monkeypatch it.
    """
    from paper_ingestion.pdf_processor import PDF_STORAGE_PATH  # noqa: PLC0415

    return Path(PDF_STORAGE_PATH) / f"{paper_id}.pdf"


def _pdf_page_sizes_sync(pdf_path: str, pages: list[int]) -> dict[int, tuple[float, float]]:
    """Return ``{page: (width, height)}`` in PDF points for the requested pages.

    Opens the document once. ``pages`` are 1-indexed; pypdfium2 is 0-indexed.
    Pages outside the document are omitted. CPU/IO-bound — call via
    ``asyncio.to_thread`` (see :func:`_get_page_sizes`).
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 — heavy native dep, import lazily

    pdf = pdfium.PdfDocument(pdf_path)
    sizes: dict[int, tuple[float, float]] = {}
    try:
        page_count = len(pdf)
        for page in set(pages):
            idx = page - 1
            if 0 <= idx < page_count:
                pdf_page = pdf[idx]
                try:
                    width, height = pdf_page.get_size()
                    sizes[page] = (float(width), float(height))
                finally:
                    pdf_page.close()
    finally:
        pdf.close()
    return sizes


async def _get_page_sizes(paper_id: int, pages: list[int]) -> dict[int, tuple[float, float]]:
    """Resolve page ``(width, height)`` for a paper, off the event loop.

    Returns an empty dict when the PDF is absent on disk (the caller then maps
    the missing geometry to a ``pdf_unavailable`` status). The same on-disk PDF
    is the one uploaded to Zotero, so its page sizes match the rendered reader.
    """
    pdf_path = _paper_pdf_path(paper_id)
    if not pdf_path.exists():
        return {}
    return await asyncio.to_thread(_pdf_page_sizes_sync, str(pdf_path), pages)


def _pick_pdf_attachment(children: list[dict[str, Any]]) -> str | None:
    """Return the key of an existing PDF attachment child that holds a stored file.

    Only ``imported_file`` PDFs are annotatable by the Zotero reader, so they
    win; a non-imported PDF child is used only as a last resort. A child whose
    ``data.md5`` is absent is a fileless orphan (e.g. an attachment item created
    by a push whose upload then failed) — reusing it would parent annotations to
    an attachment the reader cannot open, so it is skipped and the caller
    re-creates + uploads a fresh attachment instead.
    """
    pdfs = [
        c
        for c in children
        if (c.get("data") or {}).get("contentType") == "application/pdf"
        and (c.get("data") or {}).get("md5")
    ]
    if not pdfs:
        return None
    for child in pdfs:
        if (child.get("data") or {}).get("linkMode") == "imported_file":
            key = child.get("key") or (child.get("data") or {}).get("key")
            if key:
                return key
    first = pdfs[0]
    return first.get("key") or (first.get("data") or {}).get("key")


async def _persist_attachment_key(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    paper_id: int,
    resolved_owner_id: int | None,
    attachment_key: str,
) -> None:
    """Persist the owner's ``zotero_attachment_key`` at-most-once (NULL-guarded).

    Writes the per-user ``paper_user_zotero_links`` row (which already exists from
    the item push). The ``WHERE ... IS NULL`` guard makes a concurrent
    double-ensure idempotent; a None owner (ambiguous) matches no row — a safe no-op.
    """
    await conn.execute(
        "UPDATE paper_user_zotero_links SET zotero_attachment_key = $1, updated_at = NOW()"
        " WHERE paper_id = $2 AND user_id = $3 AND zotero_attachment_key IS NULL",
        attachment_key,
        paper_id,
        resolved_owner_id,
    )


async def _ensure_zotero_attachment(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    client: Any,
    paper_id: int,
    zotero_item_key: str,
    existing_attachment_key: str | None,
    resolved_owner_id: int | None,
) -> str:
    """Return the PDF-attachment key to parent annotations on, creating it once.

    Lazy, at-most-once per paper:
      1. reuse the persisted link ``zotero_attachment_key`` if set;
      2. else find an existing PDF attachment child of ``zotero_item_key``;
      3. else create a new ``imported_file`` attachment + upload the PDF bytes.

    Persists the resolved key (steps 2/3). Raises :class:`_AttachmentUnavailableError`
    (``pdf_unavailable`` / ``quota_exceeded`` / ``attachment_failed``) on failure.
    """
    if existing_attachment_key:
        return existing_attachment_key

    try:
        children = await client.get_item_children(zotero_item_key, item_type="attachment")
    except Exception:
        logger.warning(
            "Zotero: listing attachment children failed for paper %d", paper_id, exc_info=True
        )
        children = []
    found = _pick_pdf_attachment(children)
    if found:
        await _persist_attachment_key(conn, paper_id, resolved_owner_id, found)
        return found

    from paper_ingestion.pdf_processor import MAX_PDF_SIZE  # noqa: PLC0415

    pdf_path = _paper_pdf_path(paper_id)
    if not pdf_path.exists():
        raise _AttachmentUnavailableError("pdf_unavailable")
    if pdf_path.stat().st_size > MAX_PDF_SIZE:
        logger.error("Zotero: PDF for paper %d exceeds the %d-byte cap", paper_id, MAX_PDF_SIZE)
        raise _AttachmentUnavailableError("attachment_failed")

    try:
        result = await client.create_item(
            {
                "itemType": "attachment",
                "linkMode": "imported_file",  # only a stored file is reader-annotatable
                "parentItem": str(zotero_item_key),
                "title": "Full Text PDF",
                "contentType": "application/pdf",
                "filename": f"{paper_id}.pdf",
            }
        )
    except Exception:
        logger.error("Zotero: attachment item create failed for paper %d", paper_id, exc_info=True)
        raise _AttachmentUnavailableError("attachment_failed") from None
    attachment_key = result.get("successful", {}).get("0", {}).get("key")
    if not attachment_key:
        logger.error("Zotero: attachment create returned no key for paper %d: %s", paper_id, result)
        raise _AttachmentUnavailableError("attachment_failed")

    try:
        await client.upload_attachment(attachment_key, str(pdf_path))
    except httpx.HTTPStatusError as exc:
        status = "quota_exceeded" if exc.response.status_code == 413 else "attachment_failed"
        logger.error(
            "Zotero: PDF upload failed for paper %d (HTTP %s)",
            paper_id,
            exc.response.status_code,
            exc_info=True,
        )
        raise _AttachmentUnavailableError(status) from exc
    except Exception:
        logger.error("Zotero: PDF upload failed for paper %d", paper_id, exc_info=True)
        raise _AttachmentUnavailableError("attachment_failed") from None

    await _persist_attachment_key(conn, paper_id, resolved_owner_id, attachment_key)
    return attachment_key


def _build_annotation_item(
    *,
    parent_key: str,
    page: int,
    rect: dict[str, Any],
    note: str | None,
    color: str | None,
    quote: str | None,
    width: float,
    height: float,
) -> dict[str, Any]:
    """Assemble the Zotero ``annotation`` item body for one stored highlight.

    ``parentItem`` is the PDF **attachment** key (so the reader can open it),
    and ``annotationPosition`` carries the de-normalized, y-flipped rects.
    """
    zotero_rects = denormalize_rect_to_zotero(rect, width, height)
    page_index = page - 1
    y_top = max((r[3] for r in zotero_rects), default=0.0)  # bounding top edge
    return {
        "itemType": "annotation",
        "parentItem": str(parent_key),
        "annotationType": "highlight",
        "annotationText": quote or "",
        "annotationComment": note or "",
        "annotationPageLabel": str(page),
        "annotationColor": color or "#ffd400",
        # annotationPosition is a JSON STRING, not a native nested object.
        "annotationPosition": json.dumps({"pageIndex": page_index, "rects": zotero_rects}),
        "annotationSortIndex": build_sort_index(page_index, y_top),
    }


async def _export_one_highlight(
    db_pool: asyncpg.Pool,
    client: Any,
    *,
    highlight_id: int,
    page: int,
    rect: dict[str, Any],
    note: str | None,
    color: str | None,
    quote: str | None,
    attachment_key: str,
    width: float,
    height: float,
) -> dict[str, Any]:
    """Create one Zotero annotation for a highlight and persist its key.

    Returns ``{highlight_id, status, zotero_annotation_key?}``. The partial
    unique index enforces at-most-once push: a persist collision is treated as
    ``already_synced`` (concurrent double-push), and a create with no returned
    key is ``push_failed``.
    """
    item_data = _build_annotation_item(
        parent_key=attachment_key,
        page=page,
        rect=rect,
        note=note,
        color=color,
        quote=quote,
        width=width,
        height=height,
    )
    result = await client.create_item(item_data)
    annotation_key = result.get("successful", {}).get("0", {}).get("key")
    if not annotation_key:
        logger.error(
            "Zotero highlight push failed for highlight %d: no key in response %s",
            highlight_id,
            result,
        )
        return {"highlight_id": highlight_id, "status": "push_failed"}

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE paper_highlights SET zotero_annotation_key = $1 WHERE id = $2",
                annotation_key,
                highlight_id,
            )
    except asyncpg.UniqueViolationError:
        logger.info(
            "Zotero highlight %d already synced (key %s collided on persist)",
            highlight_id,
            annotation_key,
        )
        return {
            "highlight_id": highlight_id,
            "status": "already_synced",
            "zotero_annotation_key": annotation_key,
        }

    logger.info("Highlight %d pushed to Zotero as annotation %s", highlight_id, annotation_key)
    return {
        "highlight_id": highlight_id,
        "status": "ok",
        "zotero_annotation_key": annotation_key,
    }


async def push_highlight_to_zotero(
    highlight_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    owner_user_id: int | None = None,
) -> dict[str, Any]:
    """Push a JARVIS spatial highlight to Zotero as a child annotation item.

    One-way sync: the highlight becomes a ``highlight`` annotation on the paper's
    Zotero item. Idempotent via ``paper_highlights.zotero_annotation_key`` and the
    partial unique index ``uq_paper_highlights_zotero_key`` — a highlight that
    already carries a key, or whose key collides on persist, is a no-op.

    Returns a ``{"status": ...}`` dict; the no-op cases never raise:
      * credentials missing → ``disabled``; config undecryptable → ``config_decrypt_failed``
      * highlight not found for ``owner_user_id`` (tenancy scope) → ``not_found``
      * paper not linked to Zotero for this owner (no link row) → ``not_linked``
      * highlight already pushed → ``already_synced``
      * pushed this call → ``ok`` with the returned ``zotero_annotation_key``
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    try:
        cfg = await _get_zotero_config(db_pool, user_id=owner_user_id)
    except ZoteroConfigDecryptError:
        logger.warning(
            "Zotero config decryption failed for highlight %d push — "
            "stored credentials are unreadable (key rotation?); "
            "operator must re-save Zotero API key in Settings",
            highlight_id,
        )
        return {"highlight_id": highlight_id, "status": "config_decrypt_failed"}

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
    if not api_key or not user_id:
        return {"highlight_id": highlight_id, "status": "disabled"}

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),  # type: ignore[arg-type]
        group_id=group_id,
        http_client=http_client,
    )

    # Load the highlight scoped to its owner (tenancy) joined to the paper's
    # Zotero linkage + attachment key, then ensure the PDF attachment exists on
    # the SAME connection.
    async with db_pool.acquire() as conn:
        resolved_owner_id = await _resolve_zotero_user_id(conn, owner_user_id)
        row = await conn.fetchrow(
            """
            SELECT h.paper_id, h.page, h.rect, h.note, h.color, h.quote,
                   h.zotero_annotation_key, l.zotero_item_key, l.zotero_attachment_key
            FROM paper_highlights h
            JOIN papers p ON p.id = h.paper_id
            LEFT JOIN paper_user_zotero_links l ON l.paper_id = p.id AND l.user_id = $3
            WHERE h.id = $1 AND h.user_id = $2
            """,
            highlight_id,
            owner_user_id,
            resolved_owner_id,
        )

        if not row:
            return {"highlight_id": highlight_id, "status": "not_found"}
        if row["zotero_annotation_key"]:
            return {
                "highlight_id": highlight_id,
                "status": "already_synced",
                "zotero_annotation_key": row["zotero_annotation_key"],
            }
        zotero_item_key = row["zotero_item_key"]
        if not zotero_item_key:
            return {"highlight_id": highlight_id, "status": "not_linked"}

        try:
            attachment_key = await _ensure_zotero_attachment(
                conn,
                client,
                row["paper_id"],
                str(zotero_item_key),
                row["zotero_attachment_key"],
                resolved_owner_id,
            )
        except _AttachmentUnavailableError as exc:
            return {"highlight_id": highlight_id, "status": exc.status}

    # Page size (PDF points) for the de-normalization — sourced from the same
    # on-disk PDF that is uploaded to Zotero, so pagination/sizes match.
    sizes = await _get_page_sizes(row["paper_id"], [row["page"]])
    wh = sizes.get(row["page"])
    if wh is None:
        return {"highlight_id": highlight_id, "status": "pdf_unavailable"}

    return await _export_one_highlight(
        db_pool,
        client,
        highlight_id=highlight_id,
        page=row["page"],
        rect=row["rect"],
        note=row["note"],
        color=row["color"],
        quote=row["quote"],
        attachment_key=attachment_key,
        width=wh[0],
        height=wh[1],
    )


async def push_highlights_for_paper(
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    owner_user_id: int | None = None,
) -> dict[str, Any]:
    """Push all of a paper's unsynced in-app highlights to Zotero as annotations.

    Loops the owner's highlights that do not yet carry a Zotero key, ensuring the
    PDF attachment once (amortized across all highlights) and creating one
    ``highlight`` annotation each. Idempotent: already-synced highlights and
    persist collisions are skipped, so a re-run creates zero new annotations.

    Returns ``{paper_id, exported, skipped, failed, status}``. Short-circuits to
    ``disabled`` / ``config_decrypt_failed`` / ``not_found`` / ``not_linked`` /
    ``pdf_unavailable`` / attachment-failure statuses with zero exports.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    def _result(
        status: str, *, exported: int = 0, skipped: int = 0, failed: int = 0
    ) -> dict[str, Any]:
        return {
            "paper_id": paper_id,
            "exported": exported,
            "skipped": skipped,
            "failed": failed,
            "status": status,
        }

    try:
        cfg = await _get_zotero_config(db_pool, user_id=owner_user_id)
    except ZoteroConfigDecryptError:
        logger.warning(
            "Zotero config decryption failed for highlight export (paper %d) — "
            "stored credentials are unreadable (key rotation?); "
            "operator must re-save Zotero API key in Settings",
            paper_id,
        )
        return _result("config_decrypt_failed")

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
    if not api_key or not user_id:
        return _result("disabled")

    async with db_pool.acquire() as conn:
        resolved_owner_id = await _resolve_zotero_user_id(conn, owner_user_id)
        paper = await conn.fetchrow(
            """
            SELECT p.id, l.zotero_item_key, l.zotero_attachment_key
            FROM papers p
            LEFT JOIN paper_user_zotero_links l ON l.paper_id = p.id AND l.user_id = $2
            WHERE p.id = $1
            """,
            paper_id,
            resolved_owner_id,
        )
    if not paper:
        return _result("not_found")
    zotero_item_key = paper["zotero_item_key"]
    if not zotero_item_key:
        return _result("not_linked")

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, page, rect, note, color, quote
            FROM paper_highlights
            WHERE paper_id = $1 AND user_id = $2 AND zotero_annotation_key IS NULL
            ORDER BY id
            """,
            paper_id,
            owner_user_id,
        )
    if not rows:
        return _result("ok")

    client = ZoteroClient(
        api_key=str(api_key),
        user_id=str(user_id),
        library_type=str(library_type),  # type: ignore[arg-type]
        group_id=group_id,
        http_client=http_client,
    )

    try:
        async with db_pool.acquire() as conn:
            attachment_key = await _ensure_zotero_attachment(
                conn,
                client,
                paper_id,
                str(zotero_item_key),
                paper["zotero_attachment_key"],
                resolved_owner_id,
            )
    except _AttachmentUnavailableError as exc:
        return _result(exc.status, failed=len(rows))

    sizes = await _get_page_sizes(paper_id, [r["page"] for r in rows])
    if not sizes:
        return _result("pdf_unavailable", failed=len(rows))

    exported = skipped = failed = 0
    for r in rows:
        wh = sizes.get(r["page"])
        if wh is None:
            failed += 1
            continue
        outcome = await _export_one_highlight(
            db_pool,
            client,
            highlight_id=r["id"],
            page=r["page"],
            rect=r["rect"],
            note=r["note"],
            color=r["color"],
            quote=r["quote"],
            attachment_key=attachment_key,
            width=wh[0],
            height=wh[1],
        )
        status = outcome["status"]
        if status == "ok":
            exported += 1
        elif status == "already_synced":
            skipped += 1
        else:
            failed += 1

    if failed == 0:
        summary_status = "ok"
    elif exported or skipped:
        summary_status = "partial_failure"
    else:
        summary_status = "push_failed"
    return _result(summary_status, exported=exported, skipped=skipped, failed=failed)


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
    *,
    owner_user_id: int | None = None,
) -> dict[str, Any]:
    """Import Zotero PDF annotations into ``paper_notes`` for a linked paper.

    Imported annotations are stored as read-only notes with ``source='zotero'``
    and idempotently upserted on the partial unique index
    ``(paper_id, user_id, zotero_annotation_key) WHERE zotero_annotation_key IS NOT NULL``
    — attributed to ``owner_user_id`` (the syncing user). They are not copied
    into verified evidence or knowledge-graph tables.
    """
    from paper_ingestion.integrations.zotero_client import ZoteroClient  # noqa: PLC0415

    try:
        cfg = await _get_zotero_config(db_pool, user_id=owner_user_id)
    except ZoteroConfigDecryptError:
        logger.warning(
            "Zotero config decryption failed for annotation sync (paper %d) — "
            "stored credentials are unreadable (key rotation?); "
            "operator must re-save Zotero API key in Settings",
            paper_id,
        )
        return {"paper_id": paper_id, "imported": 0, "status": "config_decrypt_failed"}

    api_key = cfg.get("api_key", "")
    user_id = cfg.get("user_id", "")
    library_type = cfg.get("library_type", "user")
    raw_group_id = cfg.get("group_id")
    group_id = int(raw_group_id) if raw_group_id is not None else None
    if not api_key or not user_id:
        return {"paper_id": paper_id, "imported": 0, "status": "disabled"}

    async with db_pool.acquire() as conn:
        resolved_owner_id = await _resolve_zotero_user_id(conn, owner_user_id)
        paper = await conn.fetchrow(
            """
            SELECT p.id, l.zotero_item_key, p.discovered_by
            FROM papers p
            LEFT JOIN paper_user_zotero_links l ON l.paper_id = p.id AND l.user_id = $2
            WHERE p.id = $1
            """,
            paper_id,
            resolved_owner_id,
        )
    if not paper:
        return {"paper_id": paper_id, "imported": 0, "status": "not_found"}
    zotero_item_key = paper["zotero_item_key"]
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
                    ON CONFLICT (paper_id, user_id, zotero_annotation_key)
                    WHERE zotero_annotation_key IS NOT NULL
                    DO UPDATE
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
                    resolved_owner_id,
                )
                imported += 1

    return {"paper_id": paper_id, "imported": imported, "status": "ok"}


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
        await _persist_poll_cursor(db_pool, polling_user_id, new_version)

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
    ctx: ProgressContext,
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
    from jarvis_common.db_helpers import assert_paper_ownership

    paper_id: int = payload["paper_id"]
    user_id = payload.get("user_id")

    # Re-validate ownership at job execution time to prevent IDOR via queued jobs.
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

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
    """
    await ctx.update_progress(0.1, "Starting Zotero library poll")
    # Thread caller user_id through so imported papers/state/annotations
    # are attributed correctly. NULL when scheduler-cron-invoked (system poll).
    polling_user_id = payload.get("user_id")
    result = await poll_zotero_library(pool, http_client, polling_user_id=polling_user_id)
    await ctx.update_progress(1.0, "Done")
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
    from jarvis_common.db_helpers import assert_paper_ownership

    paper_id = int(payload["paper_id"])
    user_id = payload.get("user_id")

    # Re-validate ownership at job execution time to prevent IDOR via queued jobs.
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

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
    from paper_ingestion.routers.pdfs import assert_paper_pdf_visible

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
