"""Citation graph logic: fetch S2 citations, create stub papers, build graph.

Stub papers are created for external citations not in our DB, marked with
metadata->>'stub' = 'true'. They can be promoted to full papers later.
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
from jarvis_common.paper_visibility import (
    PUBLIC_VISIBILITY_SCOPE,
    require_verified_public_source,
)

from paper_ingestion.db_types import ConnLike
from paper_ingestion.models import (
    CitationFetchResponse,
    CitationGraphResponse,
    GraphEdge,
    GraphNode,
)
from paper_ingestion.queries.predicates import paper_visible_sql
from paper_ingestion.sources.semantic_scholar_source import SemanticScholarSource

logger = logging.getLogger(__name__)

# How long a citation fetch stays authoritative. Semantic Scholar is rate
# limited, so every automatic path re-reads the stored graph for this long
# rather than re-fetching. Only a fetch that reached Semantic Scholar in both
# directions starts the interval, so an outage cannot silence a paper for a
# month.
CITATION_REFRESH_INTERVAL = timedelta(days=30)


async def _refresh_stale_citations(
    db_pool: asyncpg.Pool,
    s2_source: SemanticScholarSource,
    paper_ids: list[int],
    *,
    now: datetime | None = None,
) -> None:
    """Refresh citation data whose last complete fetch has aged out.

    Shared by every automatic caller so a paper is never re-fetched on two
    different schedules. A failed fetch keeps whatever edges are already
    stored and leaves the freshness stamp untouched, so the next run retries
    the paper instead of waiting out the refresh interval.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Pool used per paper, so no connection is held across an API call.
    s2_source : SemanticScholarSource
        Client used for the refresh.
    paper_ids : list[int]
        Candidates; those fetched recently are skipped.
    now : datetime | None
        Injectable clock for tests.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, citations_fetched_at FROM papers WHERE id = ANY($1::bigint[])",
            paper_ids,
        )

    cutoff = (now or datetime.now(UTC)) - CITATION_REFRESH_INTERVAL
    stale_ids = [
        int(row["id"])
        for row in rows
        if row["citations_fetched_at"] is None or row["citations_fetched_at"] <= cutoff
    ]
    for paper_id in stale_ids:
        try:
            await sync_citations_for_paper(db_pool, s2_source, paper_id)
        except Exception:
            logger.warning(
                "Citation refresh failed for paper %d; serving the stored graph",
                paper_id,
                exc_info=True,
            )


# Nodes one graph response may carry, so the payload stays renderable.
_MAX_GRAPH_NODES = 200

# Papers the expansion may collect before it stops. Visibility filtering runs
# over the collected set and can only shrink it, so the walk needs headroom
# above the node budget to still fill a response. The multiple bounds how many
# candidates are collected, and with them the visibility and edge queries. It
# does not bound the rows one hop reads: a hub-heavy graph still fetches every
# edge touching the current frontier before the cap is consulted.
_MAX_GRAPH_CANDIDATES = _MAX_GRAPH_NODES * 5

_INSERT_CITATION_SQL = """INSERT INTO paper_citations (source_paper_id, cited_paper_id,
       citation_context, is_influential, intent)
   VALUES ($1, $2, $3, $4, $5)
   ON CONFLICT (source_paper_id, cited_paper_id) DO NOTHING
   RETURNING 1"""


def _semantic_scholar_paper_id(external_id: str, metadata: dict[str, Any]) -> str | None:
    """Translate a stored paper identifier into an S2 paper lookup identifier."""
    if external_id.startswith("local:"):
        return None

    if metadata.get("s2_id"):
        return str(metadata["s2_id"])

    if external_id.startswith("s2:"):
        return external_id.removeprefix("s2:")
    if external_id.startswith("doi:"):
        return f"DOI:{external_id.removeprefix('doi:')}"
    if external_id.startswith("openalex:"):
        if metadata.get("doi"):
            return f"DOI:{metadata['doi']}"
        openalex_id = external_id.removeprefix("openalex:")
        return f"URL:https://openalex.org/{openalex_id}"
    return external_id


async def get_or_create_stub_paper(conn: ConnLike, s2_data: dict) -> int | None:
    """Look up an existing paper by S2 external_id, or insert a minimal stub row.

    Stubs are identified by ``metadata->>'stub' = 'true'`` and carry only
    title, authors, year, and citation count (no PDF, no abstract).  If the
    paper already exists (matched on ``external_id``), its citation count is
    updated via ``ON CONFLICT ... DO UPDATE`` but no other columns change.

    The function tolerates several S2 response shapes: the paper payload may
    be nested under ``citingPaper``, ``citedPaper``, or at the top level.

    This function is called only with responses obtained by the configured
    Semantic Scholar adapter, so a NEW stub row inserts with public visibility.
    An existing row (any scope) is never promoted or content-overwritten; only
    its ``citation_count`` — a trusted scholarly signal — is refreshed.

    Returns ``None`` (without raising) when the S2 payload lacks a valid
    ``paperId`` or a non-empty ``title``, so callers can safely skip the entry.
    """
    # The S2 citation response wraps the paper in a "citingPaper" or "citedPaper" key
    paper_data = s2_data.get("citingPaper") or s2_data.get("citedPaper") or s2_data

    paper_id = paper_data.get("paperId")
    title = (paper_data.get("title") or "").strip()
    if not paper_id or not title:
        return None

    external_id = f"s2:{paper_id}"

    # No pre-check SELECT: the INSERT ... ON CONFLICT DO UPDATE below handles both
    # the new-row and existing-row cases, and crucially refreshes citation_count
    # on every call (a SELECT short-circuit left it stale).

    # Build authors list
    authors = [a.get("name", "") for a in (paper_data.get("authors") or []) if a.get("name")]

    # Parse publication date
    published_date: date | None = None
    if paper_data.get("year"):
        published_date = date(paper_data["year"], 1, 1)

    url = f"https://www.semanticscholar.org/paper/{paper_id}"
    citation_count = paper_data.get("citationCount") or 0

    # Build metadata
    metadata: dict[str, Any] = {"s2_id": paper_id, "stub": "true"}
    external_ids = paper_data.get("externalIds") or {}
    if external_ids.get("ArXiv"):
        metadata["arxiv_id"] = external_ids["ArXiv"]
    if external_ids.get("DOI"):
        metadata["doi"] = external_ids["DOI"]

    require_verified_public_source("semantic_scholar")
    row = await conn.fetchrow(
        """INSERT INTO papers (external_id, source_type, title, authors, abstract,
                               published_date, url, citation_count, metadata,
                               discovery_origin, visibility_scope)
           VALUES ($1, 'semantic_scholar', $2, $3, '', $4, $5, $6, $7::jsonb,
                   'citation_batch', $8)
           ON CONFLICT (external_id) DO UPDATE SET
               citation_count = EXCLUDED.citation_count
           RETURNING id""",
        external_id,
        title,
        authors,
        published_date,
        url,
        citation_count,
        metadata,
        PUBLIC_VISIBILITY_SCOPE,
    )
    return row["id"] if row else None


async def _sync_citation_direction(
    conn: ConnLike,
    items: list[dict[str, Any]],
    seed_paper_id: int,
    *,
    related_paper_is_source: bool,
) -> tuple[int, int]:
    """Persist one citation direction and count inserted edges and created stubs."""
    added = 0
    stubs_created = 0

    for item in items:
        related_paper_id = await get_or_create_stub_paper(conn, item)
        if related_paper_id is None or related_paper_id == seed_paper_id:
            continue

        contexts = item.get("contexts") or []
        source_paper_id = related_paper_id if related_paper_is_source else seed_paper_id
        cited_paper_id = seed_paper_id if related_paper_is_source else related_paper_id
        inserted = await conn.fetchval(
            _INSERT_CITATION_SQL,
            source_paper_id,
            cited_paper_id,
            contexts[0] if contexts else None,
            item.get("isInfluential"),
            item.get("intents") or [],
        )
        if inserted is None:
            continue

        added += 1
        stub_check = await conn.fetchval(
            "SELECT metadata->>'stub' FROM papers WHERE id = $1", related_paper_id
        )
        if stub_check == "true":
            stubs_created += 1

    return added, stubs_created


async def sync_bibliography_references(
    conn: ConnLike,
    references: list[dict[str, Any]],
    paper_id: int,
) -> tuple[int, int]:
    """Persist resolved bibliography references through citation insertion.

    Parameters
    ----------
    conn : ConnLike
        Connection used only after all provider lookups have completed.
    references : list[dict[str, Any]]
        Semantic Scholar paper payloads for references the parser resolved.
    paper_id : int
        Uploaded paper that cites every supplied reference.

    Returns
    -------
    tuple[int, int]
        Inserted edge count and number of referenced stub papers.
    """
    return await _sync_citation_direction(
        conn,
        references,
        paper_id,
        related_paper_is_source=False,
    )


async def sync_citations_for_paper(
    conn_or_pool: ConnLike | asyncpg.Pool,
    s2_source: SemanticScholarSource,
    paper_id: int,
) -> CitationFetchResponse:
    """Fetch citing and referenced papers from Semantic Scholar and persist edges.

    Accepts either an ``asyncpg.Pool`` (preferred) or a bare connection.
    When a pool is passed, S2 API calls happen **outside** any DB connection
    scope so that long-running HTTP requests do not hold a connection.

    For each citation/reference the function creates a stub paper via
    :func:`get_or_create_stub_paper` and inserts a ``paper_citations`` row
    (skipped on duplicate via ``ON CONFLICT DO NOTHING``).

    Graceful degradation:
    * If the S2 API call for citations *or* references fails, that direction
      is skipped (logged) and the other direction still proceeds.
    * If the ``paper_citations`` table does not exist yet, returns zeroes
      instead of raising; only the missing-paper precondition still raises
      ``ValueError``.

    The paper's ``citations_fetched_at`` timestamp is set only when both
    directions were fetched, so a caller can avoid redundant fetches without a
    failed one passing for fresh data. A paper with no Semantic Scholar
    identifier returns zeroes without touching the timestamp.

    Raises
    ------
    ValueError
        If *paper_id* does not exist in the ``papers`` table.
    """
    is_pool = isinstance(conn_or_pool, asyncpg.Pool)

    # --- Look up the paper's S2 ID (short DB read) ---
    async def _lookup_paper() -> asyncpg.Record | None:
        if is_pool:
            async with conn_or_pool.acquire() as c:  # type: ignore[union-attr]
                return await c.fetchrow(
                    "SELECT external_id, metadata FROM papers WHERE id = $1", paper_id
                )
        return await conn_or_pool.fetchrow(  # type: ignore[union-attr]
            "SELECT external_id, metadata FROM papers WHERE id = $1", paper_id
        )

    paper = await _lookup_paper()
    if not paper:
        raise ValueError(f"Paper {paper_id} not found")

    external_id = str(paper["external_id"] or "")
    metadata = paper["metadata"] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    s2_id = _semantic_scholar_paper_id(external_id, metadata)
    if s2_id is None:
        return CitationFetchResponse(citations_added=0, references_added=0, stubs_created=0)

    # --- S2 API calls (no DB connection held) ---
    fetch_complete = True
    try:
        citations_data = await s2_source.fetch_citations(s2_id)
    except Exception:
        logger.exception("Failed to fetch citations for paper %d (s2:%s)", paper_id, s2_id)
        citations_data = []
        fetch_complete = False

    try:
        references_data = await s2_source.fetch_references(s2_id)
    except Exception:
        logger.exception("Failed to fetch references for paper %d (s2:%s)", paper_id, s2_id)
        references_data = []
        fetch_complete = False

    # --- DB writes with fetched data (connection held, no HTTP) ---
    citations_added = 0
    references_added = 0
    stubs_created = 0

    async def _persist(conn: ConnLike) -> tuple[int, int, int]:
        nonlocal citations_added, references_added, stubs_created

        try:
            citations_added, citation_stubs_created = await _sync_citation_direction(
                conn,
                citations_data,
                paper_id,
                related_paper_is_source=True,
            )
            stubs_created += citation_stubs_created
        except asyncpg.exceptions.UndefinedTableError:
            logger.warning("paper_citations table not found, skipping citation sync")
            return 0, 0, 0

        try:
            references_added, reference_stubs_created = await _sync_citation_direction(
                conn,
                references_data,
                paper_id,
                related_paper_is_source=False,
            )
            stubs_created += reference_stubs_created
        except asyncpg.exceptions.UndefinedTableError:
            logger.warning("paper_citations table not found, skipping citation sync")
            return 0, 0, 0

        # Freshness is claimed only for a fetch that reached Semantic Scholar in
        # both directions. Stamping a failed fetch would hide the paper from the
        # staleness sweep for a full refresh interval.
        if fetch_complete:
            await conn.execute(
                "UPDATE papers SET citations_fetched_at = $1 WHERE id = $2",
                datetime.now(UTC),
                paper_id,
            )
        return citations_added, references_added, stubs_created

    if is_pool:
        async with conn_or_pool.acquire() as conn:  # type: ignore[union-attr]
            citations_added, references_added, stubs_created = await _persist(conn)
    else:
        citations_added, references_added, stubs_created = await _persist(conn_or_pool)  # type: ignore[arg-type]

    return CitationFetchResponse(
        citations_added=citations_added,
        references_added=references_added,
        stubs_created=stubs_created,
    )


async def _filter_visible_paper_ids(
    conn: ConnLike,
    candidate_ids: list[int],
    user_id: int,
) -> list[int]:
    """Return the subset of *candidate_ids* visible to *user_id*.

    The shared predicate grants persisted public papers or private papers in
    the caller's library. Provenance labels and discoverer attribution do not
    authorize access.
    """
    rows = await conn.fetch(
        f"""SELECT id FROM papers
           WHERE id = ANY($1)
             AND {paper_visible_sql(2, alias="papers")}""",
        candidate_ids,
        user_id,
    )
    return [r["id"] for r in rows]


async def _collect_graph_candidates(
    conn: ConnLike,
    paper_ids: list[int],
    depth: int,
) -> list[int]:
    """Walk ``paper_citations`` from *paper_ids* and return the papers reached.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds.
    paper_ids : list[int]
        Seed papers the walk starts from.
    depth : int
        Number of hops to expand.

    Returns
    -------
    list[int]
        Seeds in the order given, then each hop, ascending by ID within a hop,
        so the same request collects the same papers in the same order. The
        walk stops once ``_MAX_GRAPH_CANDIDATES`` papers are collected.

    Raises
    ------
    asyncpg.exceptions.UndefinedTableError
        If the ``paper_citations`` table does not exist.
    """
    ordered: list[int] = []
    seen: set[int] = set()
    frontier: list[int] = []
    for paper_id in paper_ids:
        if paper_id not in seen:
            seen.add(paper_id)
            ordered.append(paper_id)
            frontier.append(paper_id)

    for _ in range(depth):
        if not frontier or len(ordered) >= _MAX_GRAPH_CANDIDATES:
            break
        rows = await conn.fetch(
            """SELECT source_paper_id, cited_paper_id
               FROM paper_citations
               WHERE source_paper_id = ANY($1) OR cited_paper_id = ANY($1)""",
            frontier,
        )
        discovered: set[int] = set()
        for row in rows:
            discovered.add(row["source_paper_id"])
            discovered.add(row["cited_paper_id"])
        next_frontier: list[int] = []
        for paper_id in sorted(discovered - seen):
            if len(ordered) >= _MAX_GRAPH_CANDIDATES:
                break
            seen.add(paper_id)
            ordered.append(paper_id)
            next_frontier.append(paper_id)
        frontier = next_frontier

    return ordered


async def build_citation_graph(
    conn: ConnLike,
    paper_ids: list[int],
    depth: int = 1,
    *,
    user_id: int | None = None,
) -> CitationGraphResponse:
    """Expand seed papers into a citation sub-graph up to *depth* hops.

    Starting from *paper_ids* the function iteratively discovers neighbours
    via ``paper_citations`` edges, collecting both ``source_paper_id`` and
    ``cited_paper_id`` at each hop.

    Constraints / non-obvious behaviour:
    * Expansion stops after ``_MAX_GRAPH_CANDIDATES`` papers and the response
      carries at most ``_MAX_GRAPH_NODES`` of them, seeds first and then by
      hop, ascending by ID within a hop. The node budget is applied after the
      visibility filter, so the same request returns the same graph and a
      paper the caller cannot see never occupies a node slot.
    * ``display_size`` on each node is derived from ``citation_count`` and
      clamped to [15, 40].
    * If the ``paper_citations`` table is missing, returns an empty graph
      without raising.
    * An empty *paper_ids* list short-circuits to an empty graph.
    * When *user_id* is provided, BFS-discovered nodes are filtered to only
      include papers visible to that user (prevents cross-user paper
      enumeration at BFS depth ≥1).
    """
    if not paper_ids:
        return CitationGraphResponse(nodes=[], edges=[])

    try:
        candidate_ids = await _collect_graph_candidates(conn, paper_ids, depth)
    except asyncpg.exceptions.UndefinedTableError:
        return CitationGraphResponse(nodes=[], edges=[])

    # Restrict node fetch to papers visible to the caller — prevents cross-user
    # enumeration. The filter runs over the whole walk and the node budget is
    # applied afterwards, so a paper the caller cannot see never occupies a slot
    # that a visible one, discovered later in the walk, would have taken.
    if user_id is not None:
        visible_ids = set(await _filter_visible_paper_ids(conn, candidate_ids, user_id))
        candidate_ids = [pid for pid in candidate_ids if pid in visible_ids]

    all_ids = candidate_ids[:_MAX_GRAPH_NODES]
    if not all_ids:
        return CitationGraphResponse(nodes=[], edges=[])

    # Fetch node data
    node_rows = await conn.fetch(
        """SELECT id, title, citation_count, published_date, metadata
           FROM papers WHERE id = ANY($1)""",
        all_ids,
    )
    rows_by_id = {r["id"]: r for r in node_rows}
    nodes = [
        GraphNode(
            id=r["id"],
            title=r["title"],
            citation_count=r["citation_count"] or 0,
            published_date=r["published_date"],
            is_stub=(r["metadata"] or {}).get("stub") == "true",
            display_size=min(40, max(15, 15 + (r["citation_count"] or 0) // 10)),
        )
        for r in (rows_by_id[pid] for pid in all_ids if pid in rows_by_id)
    ]

    # Fetch edges between collected nodes
    try:
        edge_rows = await conn.fetch(
            """SELECT source_paper_id, cited_paper_id, is_influential, citation_context
               FROM paper_citations
               WHERE source_paper_id = ANY($1) AND cited_paper_id = ANY($1)""",
            all_ids,
        )
    except asyncpg.exceptions.UndefinedTableError:
        edge_rows = []
    edges = [
        GraphEdge(
            source=r["source_paper_id"],
            target=r["cited_paper_id"],
            is_influential=r["is_influential"],
            context=r["citation_context"],
        )
        for r in edge_rows
    ]

    return CitationGraphResponse(nodes=nodes, edges=edges)
