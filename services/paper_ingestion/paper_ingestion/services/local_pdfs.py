"""Local PDF directory scan/import service."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from jarvis_common.jobs import JobError

from paper_ingestion.pdf_processor import MAX_PDF_SIZE, PDF_STORAGE_PATH

LOCAL_PDF_SCAN_DIR = os.environ.get("LOCAL_PDF_SCAN_DIR", "/data/local_pdfs")


async def scan_local_pdf_directory(db_pool: Any, *, scan_dir: str | None = None) -> dict[str, int]:
    """Scan the local PDF drop directory and import new valid PDFs.

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
        external_id = f"local:{file_hash[:16]}"

        async with db_pool.acquire() as file_conn:
            existing = await file_conn.fetchrow(
                "SELECT id FROM papers WHERE external_id = $1", external_id
            )
            if existing:
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
                async with file_conn.transaction():
                    row = await file_conn.fetchrow(
                        """
                        INSERT INTO papers (external_id, source_type, title, authors, abstract,
                                            url, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        RETURNING *
                        """,
                        external_id,
                        "local",
                        title,
                        [],
                        None,
                        f"local://{file_hash}",
                        {},
                    )
                    paper_id = row["id"]
                    final_path = storage_path / f"{paper_id}.pdf"
                    dest_path.rename(final_path)
                    dest_path = final_path
                    await file_conn.execute(
                        """
                        UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $1
                        WHERE id = $2
                        """,
                        str(dest_path),
                        paper_id,
                    )
            except Exception:
                dest_path.unlink(missing_ok=True)
                skipped += 1
                continue

        imported += 1

    return {"scanned": scanned, "imported": imported, "skipped": skipped}
