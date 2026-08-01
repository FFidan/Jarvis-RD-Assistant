"""Local PDF directory scan/import service."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any

from jarvis_common.jobs import JobError
from jarvis_common.library import add_to_library

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.pdf_processor import (
    MAX_PDF_SIZE,
    PDF_STORAGE_PATH,
    PDFPublishBlockedError,
    pdf_publish_operation,
)

LOCAL_PDF_SCAN_DIR = get_paper_ingestion_settings().local_pdf_scan_dir

logger = logging.getLogger(__name__)


async def scan_local_pdf_directory(
    db_pool: Any,
    *,
    user_id: int | None = None,
    scan_dir: str | None = None,
) -> dict[str, int]:
    """Scan the local PDF drop directory and import new valid PDFs.

    Parameters
    ----------
    db_pool:
        asyncpg Pool used for all DB operations.
    user_id:
        When provided, record this user as the import initiator and add the
        paper to their library. Discovery attribution is audit metadata; the
        library row grants access to this private-by-default import.
    scan_dir:
        Override the configured scan directory (used in tests).

    Returns a summary dict with ``scanned``, ``imported``, and ``skipped``.
    Raises ``JobError`` when the configured directory does not exist so the
    background job exposes a structured failure instead of silent success.
    """
    scan_path = Path(scan_dir or LOCAL_PDF_SCAN_DIR)
    if not scan_path.is_dir():
        raise JobError(f"Scan directory does not exist: {scan_path}")

    pdf_files = list(scan_path.glob("*.pdf"))
    scanned = len(pdf_files)
    imported = 0
    skipped = 0

    storage_path = Path(PDF_STORAGE_PATH)
    storage_path.mkdir(parents=True, exist_ok=True)

    for pdf_file in pdf_files:
        if pdf_file.is_symlink():
            skipped += 1
            continue
        try:
            file_size = pdf_file.stat().st_size
        except OSError:
            skipped += 1
            continue
        if file_size > MAX_PDF_SIZE:
            skipped += 1
            continue

        try:
            content = await asyncio.to_thread(pdf_file.read_bytes)
        except OSError:
            skipped += 1
            continue
        if not content.startswith(b"%PDF-"):
            skipped += 1
            continue

        file_hash = hashlib.sha256(content).hexdigest()
        external_id = f"local:{file_hash}"

        async with db_pool.acquire() as file_conn:
            existing = await file_conn.fetchrow(
                "SELECT id FROM papers WHERE external_id = $1", external_id
            )
            if existing:
                if user_id is not None:
                    try:
                        await add_to_library(
                            file_conn,
                            user_id=user_id,
                            paper_id=existing["id"],
                            added_via="manual_save",
                        )
                    except Exception as exc:
                        logger.warning(
                            "failed to add existing paper %s to scanning user %s library: %s",
                            existing["id"],
                            user_id,
                            exc,
                            exc_info=True,
                        )
                skipped += 1
                continue

            title = pdf_file.stem.replace("-", " ").replace("_", " ").title()
            temp_name = f"_importing_{file_hash[:16]}.pdf"
            dest_path = storage_path / temp_name
            try:
                await asyncio.to_thread(shutil.copy2, str(pdf_file), str(dest_path))
            except OSError:
                skipped += 1
                continue

            try:
                async with pdf_publish_operation(storage_path) as publication:
                    async with file_conn.transaction():
                        row = await file_conn.fetchrow(
                            """
                            INSERT INTO papers (external_id, source_type, title, authors, abstract,
                                                url, metadata, discovered_by, discovery_origin)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'user_initiated')
                            RETURNING *
                            """,
                            external_id,
                            "local",
                            title,
                            [],
                            None,
                            f"local://{file_hash}",
                            {},
                            user_id,
                        )
                        paper_id = row["id"]
                        if user_id is not None:
                            await add_to_library(
                                file_conn,
                                user_id=user_id,
                                paper_id=paper_id,
                                added_via="manual_save",
                            )
                        final_path = storage_path / f"{paper_id}.pdf"
                        await publication.promote(dest_path, final_path)
                        await file_conn.execute(
                            """
                            UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $1
                            WHERE id = $2
                            """,
                            str(final_path),
                            paper_id,
                        )
            except PDFPublishBlockedError as exc:
                dest_path.unlink(missing_ok=True)
                raise JobError(
                    "The PDF scan paused because a restore is in progress. Try again shortly."
                ) from exc
            except Exception:
                dest_path.unlink(missing_ok=True)
                skipped += 1
                continue

        imported += 1

    return {"scanned": scanned, "imported": imported, "skipped": skipped}
