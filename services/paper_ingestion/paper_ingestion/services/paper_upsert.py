"""Canonical paper upsert paths: attach-only saves and trusted public promotion.

The shared upsert statement with its two conflict policies, and the verified
promotion that discards derived content when a refresh replaces its source URL.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncpg
from jarvis_common.db_helpers import lock_paper_content_generation
from jarvis_common.paper_visibility import (
    PRIVATE_VISIBILITY_SCOPE,
    PUBLIC_VISIBILITY_SCOPE,
    VisibilityScope,
    require_verified_public_source,
)

from paper_ingestion.db_types import ConnLike

if TYPE_CHECKING:
    from paper_ingestion.models import PaperCreate

logger = logging.getLogger(__name__)


# Conflict policies for the shared canonical upsert. Both are trusted code
# literals interpolated verbatim into the SQL — never request-derived input.
#
# _ATTACH_ONLY_CONFLICT: the unverified client path (batch-save, Zotero). On
# conflict it mutates nothing canonical; the no-op self-assignment returns the
# existing row unchanged while `(xmax = 0)` still reports is_insert correctly.
_ATTACH_ONLY_CONFLICT = "DO UPDATE SET external_id = papers.external_id"

# _TRUSTED_REFRESH_CONFLICT: the server-owned adapter path (search/pulse/
# auto-fetch, require_verified_public_source-guarded). On conflict the trusted
# adapter re-owns EVERY client-provided descriptive column from EXCLUDED and
# forces public scope. The assignments are unconditional (no COALESCE): a
# COALESCE would let a client-supplied value survive when the adapter's value
# is NULL. Columns not listed are preserved: `discovered_by` (insert-only audit
# identity), `discovery_origin` (a non-authorizing 4-value enum, immutable after
# insert), `external_id` (the conflict key), and the server-local processing
# columns absent from the INSERT list. Those processing columns and the paper's
# chunk rows describe content derived from the row's PREVIOUS `pdf_url`, so this
# clause alone does not leave a promoted row self-consistent —
# `upsert_verified_public_paper` discards that derived content separately.
_TRUSTED_REFRESH_CONFLICT = (
    "DO UPDATE SET "
    "source_type = EXCLUDED.source_type, "
    "title = EXCLUDED.title, "
    "authors = EXCLUDED.authors, "
    "abstract = EXCLUDED.abstract, "
    "published_date = EXCLUDED.published_date, "
    "url = EXCLUDED.url, "
    "pdf_url = EXCLUDED.pdf_url, "
    "citation_count = EXCLUDED.citation_count, "
    "metadata = EXCLUDED.metadata, "
    "visibility_scope = 'public'"
)


async def _run_paper_upsert(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
    visibility_scope: VisibilityScope,
    on_conflict: str,
) -> asyncpg.Record:
    """Execute the shared canonical paper upsert with a caller-selected conflict policy.

    Parameters
    ----------
    conn : ConnLike
        Active database connection or compatible transaction proxy.
    paper : PaperCreate
        Validated descriptive paper metadata inserted for a new row.
    discovered_by : int | None
        Optional audit identity recorded only on initial insertion.
    visibility_scope : VisibilityScope
        Scope written for a NEW row. The scope of an EXISTING row on conflict is
        governed by *on_conflict*, not by this value.
    on_conflict : str
        Trusted ``ON CONFLICT (external_id)`` action clause, interpolated
        verbatim into the SQL. It MUST be a module-level code literal, never
        request-derived input: either :data:`_ATTACH_ONLY_CONFLICT` (mutate
        nothing canonical on the existing row) or :data:`_TRUSTED_REFRESH_CONFLICT`
        (re-own the full descriptive surface and force public scope).

    Returns
    -------
    asyncpg.Record
        The inserted or existing row, including ``(xmax = 0) AS is_insert``.
    """
    row = await conn.fetchrow(
        f"""INSERT INTO papers (external_id, source_type, title, authors, abstract,
                               published_date, url, pdf_url, citation_count, metadata,
                               discovery_origin, discovered_by, visibility_scope)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
           ON CONFLICT (external_id) {on_conflict}
           RETURNING *, (xmax = 0) AS is_insert""",
        paper.external_id,
        paper.source_type.value,
        paper.title,
        paper.authors,
        paper.abstract,
        paper.published_date,
        paper.url,
        paper.pdf_url,
        paper.citation_count,
        paper.metadata,
        paper.discovery_origin,
        discovered_by,
        visibility_scope,
    )
    if row is None:
        raise RuntimeError("upsert_paper RETURNING always yields a row")
    return row


async def upsert_paper(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
) -> asyncpg.Record:
    """Insert or update a private-by-default canonical paper.

    Parameters
    ----------
    conn : ConnLike
        Active database connection or compatible transaction proxy.
    paper : PaperCreate
        Validated descriptive paper metadata. Its source label does not grant
        public visibility.
    discovered_by : int | None
        Optional audit identity recorded only on initial insertion.

    Returns
    -------
    asyncpg.Record
        Inserted or updated canonical paper row.

    Notes
    -----
    On conflict this path is attach-only: it mutates no canonical column of the
    existing shared row — neither content nor scope. Callers that want a user to
    access an existing row must add `user_library` membership.
    """
    return await _run_paper_upsert(
        conn,
        paper,
        discovered_by=discovered_by,
        visibility_scope=PRIVATE_VISIBILITY_SCOPE,
        on_conflict=_ATTACH_ONLY_CONFLICT,
    )


# Pre-image: the source URL as it stood inside the promotion transaction,
# read before the upsert overwrites it.
_PRE_PROMOTION_STATE_SQL = "SELECT id, pdf_url FROM papers WHERE external_id = $1 FOR UPDATE"
_DELETE_DERIVED_CHUNKS_SQL = "DELETE FROM paper_chunks WHERE paper_id = $1"
# `is_insert` is re-projected so the returned record keeps the shape callers get
# from the upsert itself; this statement only ever runs on an existing row.
_RESET_DERIVED_PDF_STATE_SQL = (
    "UPDATE papers "
    "SET pdf_downloaded = FALSE, pdf_local_path = NULL, chunked_at = NULL, "
    "content_generation = content_generation + 1 "
    "WHERE id = $1 "
    "RETURNING *, false AS is_insert"
)


def _promotion_supersedes_derived_content(
    prior: asyncpg.Record | None,
    promoted: asyncpg.Record,
    pdf_url: str | None,
) -> bool:
    """Report whether a promotion left the row's PDF-derived content out of date.

    Parameters
    ----------
    prior : asyncpg.Record | None
        ``pdf_url`` read in the same transaction immediately before the upsert,
        or ``None`` if no row existed then.
    promoted : asyncpg.Record
        Row returned by the upsert, carrying ``is_insert``.
    pdf_url : str | None
        Source URL the trusted adapter has just written.

    Returns
    -------
    bool
        True when the derived content must be discarded.

    Notes
    -----
    A fresh insert has no derived content. The four verified adapters either
    emit no PDF URL or one concrete document URL. In particular, arXiv strips
    the revision from its canonical external id while retaining it in the PDF
    URL; Semantic Scholar and OpenAlex can move the selected open-access
    location. None supplies a content fingerprint, so a changed non-empty URL
    must be treated as potentially different bytes rather than normalized away.
    A conflict with no pre-image is a concurrent insert: that row is
    microseconds old and has nothing derived to lose, so discarding is the
    cheap fail-closed answer.
    """
    if promoted["is_insert"]:
        return False
    if prior is None:
        return True
    return (prior["pdf_url"] or None) != (pdf_url or None)


def _deleted_row_count(command_status: str) -> int:
    """Return the row count carried by a PostgreSQL ``DELETE n`` command tag.

    Parameters
    ----------
    command_status : str
        Status string ``conn.execute`` returns for the last command it ran.

    Returns
    -------
    int
        Rows the statement removed, or ``0`` when the tag carries no count.
        The count is only ever logged, so an unrecognized tag must not raise.
    """
    _, _, count = command_status.rpartition(" ")
    return int(count) if count.isdigit() else 0


async def upsert_verified_public_paper(
    conn: ConnLike,
    paper: PaperCreate,
    *,
    discovered_by: int | None = None,
    discarded_content_ids: list[int] | None = None,
) -> asyncpg.Record:
    """Insert or promote a paper verified by a server-owned scholarly adapter.

    Parameters
    ----------
    conn : ConnLike
        Active database connection or compatible transaction proxy.
    paper : PaperCreate
        Metadata returned by the configured server-side source adapter.
    discovered_by : int | None
        Optional audit identity recorded only on initial insertion.
    discarded_content_ids : list[int] | None
        Optional collector. The paper's id is appended once the block below has
        exited after discarding its derived content, so the caller can reclaim
        the matching vector points and files outside this transaction with
        :func:`reclaim_discarded_paper_content`.

    Returns
    -------
    asyncpg.Record
        Inserted or updated canonical paper row with public visibility.

    Raises
    ------
    ValueError
        If `paper.source_type` is not in the centralized verified-public
        adapter set.

    Notes
    -----
    Authenticated request payloads must use :func:`upsert_paper`. The source
    guard is defense in depth; call-site ownership of the trusted adapter
    boundary is also covered by tests. On conflict this path re-owns every
    client-provided descriptive column and forces public scope. Descriptive
    columns are all the conflict clause covers, so whenever a refresh replaces
    its ``pdf_url``, the content derived from the superseded URL is discarded
    here: the paper's ``paper_chunks`` rows, ``pdf_downloaded``,
    ``pdf_local_path`` and ``chunked_at``. The paper's content generation is
    incremented in that same transaction so existing highlights become stale
    at the exact commit that retires their source document. The returned record
    reports that reset, so a caller echoing it cannot publish a stale local PDF
    pointer.
    Qdrant vectors and the stored PDF file are not modified by this function;
    removing the chunk rows is what forces a re-process. Their storage is
    reclaimed by the caller through ``discarded_content_ids``, after the
    transaction that owns the refresh commits.

    Maintenance constraint: the trusted refresh and the chunk-row delete belong
    to the single transaction opened here, so both become visible in the same
    commit. Retrieval in :mod:`paper_ingestion.rag.streaming` serves only
    excerpts a stored chunk row still backs, and reads those keys on a
    connection of its own — before the vector round trip on the single-paper
    path, after it on the cross-paper one. Neither ordering carries the
    guarantee: it rests on there being no committed state in which the new
    source URL is visible while its discarded chunk rows remain. Moving either
    statement into its own transaction — for example to shorten how long the
    row lock is held — creates that state and changes what those reads can
    observe.
    """
    require_verified_public_source(paper.source_type.value)
    discarded_id: int | None = None
    async with conn.transaction():
        prior = await conn.fetchrow(_PRE_PROMOTION_STATE_SQL, paper.external_id)
        if prior is not None and (prior["pdf_url"] or None) != (paper.pdf_url or None):
            await lock_paper_content_generation(conn, int(prior["id"]))
        row = await _run_paper_upsert(
            conn,
            paper,
            discovered_by=discovered_by,
            visibility_scope=PUBLIC_VISIBILITY_SCOPE,
            on_conflict=_TRUSTED_REFRESH_CONFLICT,
        )
        if _promotion_supersedes_derived_content(prior, row, paper.pdf_url):
            if prior is None:
                await lock_paper_content_generation(conn, int(row["id"]))
            status = await conn.execute(_DELETE_DERIVED_CHUNKS_SQL, row["id"])
            logger.info(
                "Promotion discarded %d derived chunk row(s) for paper %d (%s)",
                _deleted_row_count(status),
                row["id"],
                row["external_id"],
            )
            reset = await conn.fetchrow(_RESET_DERIVED_PDF_STATE_SQL, row["id"])
            if reset is None:
                raise RuntimeError("derived-content reset RETURNING always yields a row")
            row, discarded_id = reset, int(reset["id"])
    # Recorded only after the block above ended, so an id never survives a
    # statement that the block then rolled back.
    if discarded_id is not None and discarded_content_ids is not None:
        discarded_content_ids.append(discarded_id)
    return row
