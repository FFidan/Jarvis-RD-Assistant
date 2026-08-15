"""Row-to-model conversion helpers for the paper ingestion service.

Centralizes the mapping from asyncpg Record objects to Pydantic response
models so that routers and main.py share a single implementation.
"""

from datetime import UTC
from typing import Any

import asyncpg
from jarvis_common.paper_visibility import PUBLIC_VISIBILITY_SCOPE

from paper_ingestion.db_types import ConnLike
from paper_ingestion.models import (
    ChunkResponse,
    CrossReference,
    FeedPaper,
    KeyFinding,
    PaperResponse,
    RecentFeedback,
    SourceType,
    SummaryResponse,
)
from paper_ingestion.queries.predicates import paper_visible_sql


def _base_paper_fields(row: asyncpg.Record) -> dict[str, Any]:
    """Field assignments shared verbatim by the paper and feed converters.

    Hoisted into one place so ``row_to_paper_response`` and
    ``row_to_feed_paper`` cannot drift apart when a shared column is added or
    renamed; a past field addition landed in only one of the two.
    """
    return {
        "id": row["id"],
        "external_id": row["external_id"],
        "source_type": row["source_type"],
        "title": row["title"],
        "authors": row["authors"],
        "abstract": row["abstract"],
        "published_date": row["published_date"],
        "url": row["url"],
        "pdf_url": row["pdf_url"],
        "pdf_local_path": row["pdf_local_path"],
        "pdf_downloaded": row["pdf_downloaded"],
        "citation_count": row["citation_count"],
        "metadata": row["metadata"] or {},
        "discovered_at": row.get("discovered_at"),
        "created_at": row["created_at"],
    }


def row_to_paper_response(row: asyncpg.Record) -> PaperResponse:
    """Convert an asyncpg Record from the ``papers`` table to a PaperResponse.

    The ``priority_score`` field is populated **only** when the query includes
    the ``priority_score`` column (e.g. via a JOIN or computed expression).
    If the column is absent from the Record's keys the field defaults to
    ``None``.  This means callers that use ``SELECT *`` on the base ``papers``
    table will get the stored value, while callers that project a subset of
    columns will silently omit it.  ``discovered_at`` and ``discovery_origin``
    are guarded for the same reason (either may be absent from a projected
    subset), each falling back to the model default.
    """
    return PaperResponse(
        **_base_paper_fields(row),
        priority_score=row["priority_score"] if "priority_score" in row.keys() else None,
        discovery_origin=(
            row["discovery_origin"] if "discovery_origin" in row.keys() else "user_initiated"
        ),
    )


def row_to_feed_paper(row: asyncpg.Record) -> FeedPaper:
    """Convert a joined papers+summaries+user_state row to FeedPaper."""
    recent_feedback = None
    if row.get("recent_feedback_signal") is not None:
        recent_feedback = RecentFeedback(
            signal=row["recent_feedback_signal"],
            source=row["recent_feedback_source"],
            created_at=row["recent_feedback_created_at"],
        )
    return FeedPaper(
        **_base_paper_fields(row),
        priority_score=row.get("priority_score"),
        discovery_origin=row.get("discovery_origin", "user_initiated"),
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
        recent_feedback=recent_feedback,
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


async def filter_current_cross_references(
    conn: ConnLike,
    cross_references: list[dict],
    requester_id: int | None,
) -> list[dict]:
    """Enrich current references and retain a usable entry for a missing target.

    Visibility is re-checked on every read rather than trusted from the moment
    the reference was stored: a paper that has since left the caller's reach
    must not disclose its title, so it is treated exactly like a deleted one.
    ``requester_id`` of ``None`` is a background caller and sees public papers
    only, matching the scope the references were generated under.

    Parameters
    ----------
    conn : ConnLike
        Open connection used for the visibility-scoped lookup.
    cross_references : list[dict]
        Stored reference payloads carrying ``related_paper_id``.
    requester_id : int | None
        Authenticated caller, or ``None`` for a background job.

    Returns
    -------
    list[dict]
        References still current, each enriched with the related title and year
        when the caller may see the target.
    """
    if not cross_references:
        return []

    paper_ids = sorted({int(ref["related_paper_id"]) for ref in cross_references})
    columns = (
        "SELECT p.id, p.title, p.content_generation,"
        " EXTRACT(YEAR FROM p.published_date)::int AS published_year FROM papers p"
    )
    if requester_id is None:
        rows = await conn.fetch(
            f"{columns} WHERE p.id = ANY($1::bigint[]) AND p.visibility_scope = $2",
            paper_ids,
            PUBLIC_VISIBILITY_SCOPE,
        )
    else:
        rows = await conn.fetch(
            f"{columns} WHERE p.id = ANY($1::bigint[]) AND {paper_visible_sql(2)}",
            paper_ids,
            requester_id,
        )
    papers_by_id = {int(row["id"]): row for row in rows}
    current_references: list[dict] = []
    for ref in cross_references:
        related_paper = papers_by_id.get(int(ref["related_paper_id"]))
        if related_paper is None:
            current_references.append({**ref, "related_title": None, "related_year": None})
            continue
        if int(ref.get("content_generation", 0)) != int(related_paper["content_generation"]):
            continue
        current_references.append(
            {
                **ref,
                "related_title": related_paper.get("title"),
                "related_year": related_paper.get("published_year"),
            }
        )
    return current_references


def row_to_summary_response(
    row: asyncpg.Record,
    *,
    cross_references: list[dict] | None = None,
) -> SummaryResponse:
    """Convert an asyncpg Record from paper_summaries table to SummaryResponse."""
    key_findings_raw = row["key_findings"] or []
    cross_refs_raw = row["cross_references"] or [] if cross_references is None else cross_references

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


async def batch_hybrid_results_to_paper_responses(
    results: list[dict],
    db_pool: asyncpg.Pool,
) -> list[PaperResponse]:
    """Convert a list of hybrid-search result dicts to ``PaperResponse`` objects.

    Issues a single ``SELECT … WHERE id = ANY($1)`` instead of one query per
    result (eliminates the N+1 that ``hybrid_dict_to_paper_response`` had).
    RRF rank order from the ``results`` input is preserved.  Papers deleted
    between the search and the fetch fall back to a minimal ``PaperResponse``
    built from the lightweight search dict.
    """
    from datetime import datetime

    ids = [r["id"] for r in results]
    if not ids:
        return []

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM papers WHERE id = ANY($1::bigint[])", ids)

    by_id: dict[int, asyncpg.Record] = {row["id"]: row for row in rows}

    responses: list[PaperResponse] = []
    for result in results:
        row = by_id.get(result["id"])
        if row is not None:
            responses.append(row_to_paper_response(row))
        else:
            # Defensive: paper deleted between search and fetch
            responses.append(
                PaperResponse(
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
            )
    return responses


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
