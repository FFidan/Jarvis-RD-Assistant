"""Reclamation of the storage a discarded or erased paper leaves behind.

Frees a paper's vector points, stored PDF and rendered page images after a
promotion or a voided run discarded the content they were derived from. Every
step re-reads its premise and absorbs its own failures.

Account erasure removes storage on the other premise: the paper itself is going,
because the account being erased was its only holder, so its row and extracted
text go with its files. Those failures are not absorbed — an erasure that could
not remove a stored document must not report success.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Sequence
from pathlib import Path

import asyncpg
from jarvis_common.paths import secure_path

from paper_ingestion.db_types import ConnLike
from paper_ingestion.ingestion.embedder import delete_paper_vectors
from paper_ingestion.pdf_processor import (
    PDF_STORAGE_PATH,
    SNAPSHOT_STORAGE_PATH,
    pdf_publish_operation,
)
from paper_ingestion.services.paper_locks import _paper_mutation_connection

logger = logging.getLogger(__name__)

_DISCARDED_CONTENT_STATE_SQL = (
    "SELECT pdf_local_path IS NULL AND chunked_at IS NULL AS discarded FROM papers WHERE id = $1"
)


async def _paper_content_is_still_discarded(conn: ConnLike, paper_id: int) -> bool:
    """Re-read whether *paper_id* still stores nothing derived from a PDF.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds; this issues no acquisition of its
        own, so it never runs inside a transaction that owns other work.
    paper_id : int
        Paper whose current state decides whether reclamation may proceed.

    Returns
    -------
    bool
        True only while the row still carries no stored PDF pointer and no
        chunking timestamp. Every other answer is False, so nothing is deleted
        on a premise this could not confirm.

    Notes
    -----
    The three ways the premise can fail mean different things to an operator and
    are logged apart. A row storing content again is the routine outcome the
    deferral makes likely. A read that fails leaves the storage to the next
    promotion. A row that has gone leaves its stored PDF, page images and vector
    points behind for good, because nothing will ask for them again. ``IS NULL``
    never evaluates to NULL, so a NULL answer identifies that absent row rather
    than unset columns.
    """
    try:
        discarded = await conn.fetchval(_DISCARDED_CONTENT_STATE_SQL, paper_id)
    except Exception:  # noqa: BLE001 — best-effort reclamation; an unconfirmed premise skips
        logger.warning(
            "Reclamation premise unreadable for paper %d; leaving its content in place",
            paper_id,
            exc_info=True,
        )
        return False
    if discarded is None:
        logger.warning(
            "Paper %d is gone; its stored PDF, page images and vector points are left behind",
            paper_id,
        )
        return False
    if not discarded:
        logger.info("Skipping reclamation for paper %d: it stores derived content again", paper_id)
        return False
    return True


async def _reclaim_stored_files(conn: ConnLike, paper_id: int) -> None:
    """Free the paper's stored PDF and page images under the publication lock.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds; the premise is re-read on it once
        the lock is taken.
    paper_id : int
        Paper whose stored files are being freed.

    Notes
    -----
    The lock is the one every PDF publisher holds across promoting
    ``{paper_id}.pdf`` and committing the pointer that names it, so no
    publication can land between the read below and these deletions. A
    publisher that has not started waits and republishes afterwards; one that
    has committed is seen by the read, which then removes nothing. Restore
    maintenance owns the same lock, and refusing it here leaves the files for a
    later promotion rather than deleting from a set being swapped.

    The caller already holds its connection when this lock is taken, which is
    the order every publisher uses, so neither can hold one while waiting for
    the other.

    The two steps are independent: a failure is logged and the next still runs.
    A re-derived document can be shorter than the one it replaces, so the image
    directory goes whole rather than page by page.
    """
    async with pdf_publish_operation(Path(PDF_STORAGE_PATH)):
        if not await _paper_content_is_still_discarded(conn, paper_id):
            return
        try:
            pdf_path = secure_path(PDF_STORAGE_PATH, f"{paper_id}.pdf")
            await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
        except Exception:  # noqa: BLE001 — best-effort; the file is unreferenced
            logger.warning("Stored PDF reclamation failed for paper %d", paper_id, exc_info=True)
        try:
            snapshot_dir = secure_path(SNAPSHOT_STORAGE_PATH, str(paper_id))
            await asyncio.to_thread(shutil.rmtree, snapshot_dir)
        except FileNotFoundError:
            pass  # no page images were ever rendered for this paper
        except Exception:  # noqa: BLE001 — best-effort; the images are unreferenced
            logger.warning("Page-image reclamation failed for paper %d", paper_id, exc_info=True)


async def _reclaim_discarded_paper_content_on_connection(conn: ConnLike, paper_id: int) -> None:
    """Free a paper's discarded PDF-derived content over the caller's connection.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds. A caller holding the per-paper
        mutation lock on it passes it here so the deletions run inside that
        lock.
    paper_id : int
        Paper whose derived content a promotion has just discarded.

    Notes
    -----
    Every step is best-effort: each failure is logged, the remaining steps still
    run, and nothing reaches the caller. That matters most to the locked caller,
    for which a raised cleanup failure would replace the error it has to report.

    The premise is re-read here rather than assumed from the caller's list. The
    discard leaves exactly the state the download sweep selects on, and the
    promotion has just written the source URL it would fetch, so the paper can
    acquire new content before this runs. The state is read rather than the
    source URL because it describes what these deletions destroy: a re-download
    keeps the promoted URL, so a URL comparison would still permit removing a
    file and page images the paper currently points at.

    One read cannot govern the whole call, because a vector-store round trip
    separates it from the file steps and a download can commit inside that gap.
    The file steps therefore re-read the premise while holding the lock every
    PDF publisher holds across promoting the file and committing the pointer
    that names it, which leaves no interleaving in which they remove a file or
    page images a committed download has just produced.

    The caller holds the per-paper mutation lock on *conn*, which excludes
    every writer of this paper's deterministic vectors for the whole call. An
    aborting run's points and a concurrent successful run's points use the same
    ids, so this serialization is what makes the delete unambiguous.
    """
    try:
        if not await _paper_content_is_still_discarded(conn, paper_id):
            return
        try:
            await delete_paper_vectors(paper_id)
        except Exception:  # noqa: BLE001 — best-effort; orphan vectors are unreachable
            logger.warning("Vector reclamation failed for paper %d", paper_id, exc_info=True)
        await _reclaim_stored_files(conn, paper_id)
    except Exception:  # noqa: BLE001 — best-effort; no failure may reach the caller
        logger.warning("Reclamation failed for paper %d", paper_id, exc_info=True)


async def reclaim_discarded_paper_content(paper_id: int, db_pool: asyncpg.Pool) -> None:
    """Free the storage a paper's discarded PDF-derived content left behind.

    Removes the paper's vector points, its stored PDF file and the directory of
    page images rendered from it.

    Parameters
    ----------
    paper_id : int
        Paper whose derived content a promotion has just discarded.
    db_pool : asyncpg.Pool
        Pool supplying the locked connection the reclamation reads and deletes on.

    Notes
    -----
    Reclamation, not a security control. Nothing here decides what a reader may
    see: the promotion removes the paper's ``paper_chunks`` rows in the same
    transaction as the visibility flip, and retrieval serves only excerpts a
    stored chunk row still backs, so anything left here is wasted space rather
    than reachable content. Every step is best-effort — each failure is logged,
    the remaining steps still run, and nothing reaches the caller.

    Call this only once the transaction that discarded the content has
    committed. Qdrant and the filesystem are not transactional, so running it
    inside that transaction would destroy content a rollback still points at.

    The per-paper mutation lock spans the state read, deterministic vector
    delete, and stored-file reclamation. A publisher that finishes first is
    observed as storing content again; a publisher that starts later waits and
    writes a fresh generation after reclamation releases the lock.
    """
    try:
        async with _paper_mutation_connection(db_pool, paper_id) as conn:
            await _reclaim_discarded_paper_content_on_connection(conn, paper_id)
    except Exception:  # noqa: BLE001 — best-effort; no failure may reach the caller
        logger.warning("Reclamation failed for paper %d", paper_id, exc_info=True)


_LOCK_CANDIDATE_PAPERS_SQL = """
    SELECT paper.id FROM papers AS paper WHERE paper.id = ANY($1::int[]) FOR UPDATE
"""

_ORPHANED_PAPER_SQL = """
    SELECT paper.id
    FROM papers AS paper
    JOIN user_library AS held
      ON held.paper_id = paper.id AND held.user_id = $1
    WHERE paper.visibility_scope <> 'public'
      AND NOT EXISTS (
          SELECT 1 FROM user_library AS other
          WHERE other.paper_id = paper.id AND other.user_id <> $1
      )
      AND NOT EXISTS (
          SELECT 1 FROM pending_paper_deletions AS awaiting
          WHERE awaiting.paper_id = paper.id AND awaiting.user_id <> $1
      )
"""

_DELETE_ORPHANED_PAPERS_SQL = """
    DELETE FROM papers AS paper
    WHERE paper.id = ANY($2::int[])
      AND paper.visibility_scope <> 'public'
      AND NOT EXISTS (
          SELECT 1 FROM user_library AS other
          WHERE other.paper_id = paper.id AND other.user_id <> $1
      )
      AND NOT EXISTS (
          SELECT 1 FROM pending_paper_deletions AS awaiting
          WHERE awaiting.paper_id = paper.id AND awaiting.user_id <> $1
      )
    RETURNING paper.id
"""


async def _remove_stored_paper_files(paper_ids: Sequence[int]) -> None:
    """Delete the stored PDF and page images of each named paper."""
    for paper_id in paper_ids:
        pdf_path = secure_path(PDF_STORAGE_PATH, f"{paper_id}.pdf")
        await asyncio.to_thread(pdf_path.unlink, missing_ok=True)
        snapshot_dir = secure_path(SNAPSHOT_STORAGE_PATH, str(paper_id))
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(shutil.rmtree, snapshot_dir)


async def erase_orphaned_user_papers(conn: ConnLike, user_id: int) -> list[int]:
    """Remove the papers an erased account was the only holder of.

    Parameters
    ----------
    conn : ConnLike
        Research runtime connection the caller already holds.
    user_id : int
        Account being erased.

    Returns
    -------
    list[int]
        Papers whose row, chunks and stored files were removed.

    Notes
    -----
    A paper survives when it is persisted public, when another user still holds
    it in their library, or when another user's deletion of it is still waiting
    for Learning to acknowledge. That last case matters because the pending row
    is the only signal Learning ever gets: nothing in that schema references a
    paper by foreign key, so cascading the paper away would cut the
    reconciliation short instead of completing it.

    This rule is narrower than the vector purge's. That phase considers every
    paper, while this one only ever considers papers this account holds, so a
    paper nobody holds keeps its row, its text and its stored file after its
    vectors are gone. Reclaiming those is not this capability's job.

    Call this before ``research.erase_user_data``: the set is derived from this
    user's ``user_library`` rows, which that capability deletes.

    The delete re-applies the rule rather than trusting the set it selected. A
    paper another user claims, or the deployment publishes, in between is no
    longer this account's alone, and it stays.

    Rows go before files, and only the papers the delete actually removed have
    their files reclaimed. Unlinking first would destroy the stored document of
    a paper the re-check then keeps. Both happen inside one transaction, so a
    file that cannot be reclaimed takes the row deletions down with it and the
    papers become candidates again on the next attempt; without that, the rows
    would already be gone, the retry would find nothing to do, and the erasure
    would report itself complete with documents still on disk. The publication
    lock spans the whole operation so no publisher can write a file in between.

    The candidate rows are locked before the rule is re-applied. A library
    insert that commits while the delete waits on the row lock is invisible to
    the delete's own snapshot, so without the lock the paper would be removed
    and the other account's newly saved row would go with it.

    Reclamation failures are not absorbed. An erasure that cannot remove a
    stored document must not report success, and here it does not: the
    transaction unwinds and the work is still outstanding.
    """
    candidates = [int(row["id"]) for row in await conn.fetch(_ORPHANED_PAPER_SQL, user_id)]
    if not candidates:
        return []
    async with pdf_publish_operation(Path(PDF_STORAGE_PATH)), conn.transaction():
        await conn.execute(_LOCK_CANDIDATE_PAPERS_SQL, candidates)
        rows = await conn.fetch(_DELETE_ORPHANED_PAPERS_SQL, user_id, candidates)
        removed = [int(row["id"]) for row in rows]
        await _remove_stored_paper_files(removed)
    return removed
