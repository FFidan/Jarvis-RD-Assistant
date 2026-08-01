from __future__ import annotations

import hashlib
import logging

import asyncpg
import pytest

from paper_ingestion.services import local_pdfs

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


async def test_scan_links_duplicate_into_scanning_users_library(
    contract_conn, tmp_path, monkeypatch
):
    """Scanning a content-hash duplicate links it into the scanner's library."""
    from jarvis_common.testing import SharedConnPool

    # Redirect PDF storage to a writable temp dir (the scan mkdir's it); the
    # production default `/data/pdfs` is a container path and unwritable here.
    storage_dir = tmp_path / "storage"
    monkeypatch.setattr(local_pdfs, "PDF_STORAGE_PATH", str(storage_dir))

    content = b"%PDF-1.4\n" + b"x" * 64
    file_hash = hashlib.sha256(content).hexdigest()
    external_id = f"local:{file_hash}"

    # The paper is already in the corpus, discovered and held by a *different*
    # user — the scanner has no claim on it yet.
    discoverer = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('disc@local.test', 'user') RETURNING id"
    )
    scanner = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('scan@local.test', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'local', 'Existing Local PDF', ARRAY['Au'], $2, $3)
           RETURNING id""",
        external_id,
        f"local://{file_hash}",
        discoverer,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        discoverer,
        paper_id,
    )

    scanner_link_before = await contract_conn.fetchval(
        "SELECT 1 FROM user_library WHERE paper_id=$1 AND user_id=$2", paper_id, scanner
    )
    assert scanner_link_before is None, "precondition: scanner must not hold the paper yet"

    scan_dir = tmp_path / "drop"
    scan_dir.mkdir()
    (scan_dir / "dup.pdf").write_bytes(content)

    result = await local_pdfs.scan_local_pdf_directory(
        SharedConnPool(contract_conn), user_id=scanner, scan_dir=str(scan_dir)
    )

    # The duplicate is recognised (no second corpus row) but the scan still
    # grants the scanner a library membership rather than dropping the file.
    assert result["scanned"] == 1
    assert result["imported"] == 0, "no new corpus row for a content-hash duplicate"
    assert result["skipped"] == 1, "the file is counted as skipped, not imported"

    scanner_link_after = await contract_conn.fetchval(
        "SELECT 1 FROM user_library WHERE paper_id=$1 AND user_id=$2", paper_id, scanner
    )
    assert scanner_link_after == 1, "duplicate must be linked into the scanning user's library"

    # Linking the scanner is additive — the original discoverer keeps their row.
    discoverer_link = await contract_conn.fetchval(
        "SELECT 1 FROM user_library WHERE paper_id=$1 AND user_id=$2", paper_id, discoverer
    )
    assert discoverer_link == 1, "the discoverer's membership must be preserved"


async def test_scan_existing_paper_add_to_library_failure_is_logged(
    contract_conn, tmp_path, monkeypatch, caplog
):
    """When add_to_library raises, the warning is logged and the scan still completes."""
    from jarvis_common.testing import SharedConnPool

    storage_dir = tmp_path / "storage"
    monkeypatch.setattr(local_pdfs, "PDF_STORAGE_PATH", str(storage_dir))

    content = b"%PDF-1.4\n" + b"x" * 64
    file_hash = hashlib.sha256(content).hexdigest()
    external_id = f"local:{file_hash}"

    # Insert a user and a pre-existing corpus paper so the scan hits the already-exists path.
    scanner = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('faillog@local.test', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ($1, 'local', 'Existing Local PDF', ARRAY['Au'], $2, $3)
           RETURNING id""",
        external_id,
        f"local://{file_hash}",
        scanner,
    )

    async def _raise_pg_error(*args, **kwargs):
        raise asyncpg.PostgresError("connection error")

    monkeypatch.setattr(local_pdfs, "add_to_library", _raise_pg_error)

    scan_dir = tmp_path / "drop"
    scan_dir.mkdir()
    (scan_dir / "dup.pdf").write_bytes(content)

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.local_pdfs"):
        result = await local_pdfs.scan_local_pdf_directory(
            SharedConnPool(contract_conn), user_id=scanner, scan_dir=str(scan_dir)
        )

    # Scan completes without crashing, duplicate counted as skipped.
    assert result["scanned"] == 1
    assert result["imported"] == 0
    assert result["skipped"] == 1

    # The swallowed exception must now appear as a WARNING.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected at least one WARNING log record"
    msg = warning_records[0].getMessage()
    assert str(paper_id) in msg, f"Expected paper_id {paper_id!r} in log message: {msg!r}"
    assert str(scanner) in msg, f"Expected user_id {scanner!r} in log message: {msg!r}"
