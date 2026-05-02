"""Row-to-model conversion helpers for the paper ingestion service.

Centralizes the mapping from asyncpg Record objects to Pydantic response
models so that routers and main.py share a single implementation.
"""

from datetime import UTC

import asyncpg

from paper_ingestion.models import (
    ChunkResponse,
    CrossReference,
    FeedPaper,
    KeyFinding,
    PaperResponse,
    SourceType,
    SummaryResponse,
)


def row_to_paper_response(row: asyncpg.Record) -> PaperResponse:
    """Convert an asyncpg Record from the ``papers`` table to a PaperResponse.

    The ``priority_score`` field is populated **only** when the query includes
    the ``priority_score`` column (e.g. via a JOIN or computed expression).
    If the column is absent from the Record's keys the field defaults to
    ``None``.  This means callers that use ``SELECT *`` on the base ``papers``
    table will get the stored value, while callers that project a subset of
    columns will silently omit it.  ``discovered_at`` uses ``.get()`` with a
    safe default for the same reason (it may come from a joined expression).
    """
    return PaperResponse(
        id=row["id"],
        external_id=row["external_id"],
        source_type=row["source_type"],
        title=row["title"],
        authors=row["authors"],
        abstract=row["abstract"],
        published_date=row["published_date"],
        url=row["url"],
        pdf_url=row["pdf_url"],
        pdf_local_path=row["pdf_local_path"],
        pdf_downloaded=row["pdf_downloaded"],
        citation_count=row["citation_count"],
        priority_score=row["priority_score"] if "priority_score" in row.keys() else None,
        metadata=row["metadata"] or {},
        discovered_at=row.get("discovered_at"),
        created_at=row["created_at"],
    )


def row_to_feed_paper(row: asyncpg.Record) -> FeedPaper:
    """Convert a joined papers+summaries+user_state row to FeedPaper."""
    return FeedPaper(
        id=row["id"],
        external_id=row["external_id"],
        source_type=row["source_type"],
        title=row["title"],
        authors=row["authors"],
        abstract=row["abstract"],
        published_date=row["published_date"],
        url=row["url"],
        pdf_url=row["pdf_url"],
        pdf_local_path=row["pdf_local_path"],
        pdf_downloaded=row["pdf_downloaded"],
        citation_count=row["citation_count"],
        metadata=row["metadata"] or {},
        discovered_at=row.get("discovered_at"),
        created_at=row["created_at"],
        priority_score=row.get("priority_score"),
        summary_brief=row.get("summary_brief"),
        tldr=row.get("tldr"),
        confidence=row.get("confidence"),
        state=row.get("state", "inbox") or "inbox",
        state_before_trash=row.get("state_before_trash"),
        starred=row.get("starred", False) or False,
        rating=row.get("rating"),
        has_chunks=row.get("has_chunks", False),
        has_summary=row.get("has_summary", False),
        recommendation_score=row.get("recommendation_score"),
        recommendation_reason=row.get("recommendation_reason"),
        recommendation_modes=row.get("recommendation_modes"),
        note_match_count=row.get("note_match_count", 0) or 0,
        note_snippet=row.get("note_snippet"),
    )


def row_to_chunk_response(row: asyncpg.Record) -> ChunkResponse:
    """Convert an asyncpg Record from paper_chunks table to ChunkResponse."""
    return ChunkResponse(
        id=row["id"],
        paper_id=row["paper_id"],
        chunk_index=row["chunk_index"],
        content=row["content"],
        page_number=row["page_number"],
        start_char=row["start_char"],
        end_char=row["end_char"],
        embedding_id=row["embedding_id"],
        created_at=row["created_at"],
    )


def row_to_summary_response(row: asyncpg.Record) -> SummaryResponse:
    """Convert an asyncpg Record from paper_summaries table to SummaryResponse."""
    key_findings_raw = row["key_findings"] or []
    cross_refs_raw = row["cross_references"] or []

    return SummaryResponse(
        id=row["id"],
        paper_id=row["paper_id"],
        summary_brief=row["summary_brief"],
        summary_detailed=row["summary_detailed"],
        tldr=row.get("tldr"),
        key_findings=[KeyFinding(**f) for f in key_findings_raw],
        methodology=row["methodology"],
        limitations=row["limitations"],
        relevance_notes=row["relevance_notes"],
        confidence=row["confidence"],
        cross_references=[CrossReference(**r) for r in cross_refs_raw],
        llm_model=row["llm_model"],
        summary_verified=row["summary_verified"],
        created_at=row["created_at"],
    )


async def hybrid_dict_to_paper_response(
    result: dict,
    db_pool: asyncpg.Pool,
) -> PaperResponse:
    """Convert a hybrid-search result dict to a full ``PaperResponse``.

    The hybrid search returns a lightweight dict.  This helper fetches
    the remaining columns (pdf_local_path, pdf_downloaded, etc.) so the
    response matches the ``PaperResponse`` schema.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", result["id"])
    if row is None:
        # Defensive: paper deleted between search and fetch
        from datetime import datetime

        return PaperResponse(
            id=result["id"],
            external_id="",
            source_type=SourceType.ARXIV,
            title=result.get("title", ""),
            authors=result.get("authors", []),
            abstract=result.get("abstract"),
            published_date=result.get("published_date"),
            url=result.get("url", "https://unknown"),
            created_at=datetime.now(UTC),
        )
    return row_to_paper_response(row)


def deduplicate_by_paper_id(results: list[dict]) -> list[dict]:
    """Deduplicate results by paper_id, keeping the entry with highest score."""
    seen: dict[int, dict] = {}
    for r in results:
        pid = r.get("paper_id")
        if pid is None:
            continue
        if pid not in seen or r.get("score", 0) > seen[pid].get("score", 0):
            seen[pid] = r
    return list(seen.values())
