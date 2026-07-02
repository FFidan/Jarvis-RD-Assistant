"""Highlight export and annotation sync: in-app highlight ↔ Zotero annotation."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from paper_ingestion.integrations._zotero_config import (
    ZoteroConfigDecryptError,
    _get_zotero_config,
    _resolve_zotero_user_id,
)
from paper_ingestion.integrations.zotero_geometry import (
    build_sort_index,
    denormalize_rect_to_zotero,
)

logger = logging.getLogger("paper_ingestion.integrations.zotero_service")


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
