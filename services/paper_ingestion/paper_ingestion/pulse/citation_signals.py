"""Optional citation-graph signals for Pulse Phase 2 scoring."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def compute_citation_signals(
    db_pool: Any,
    external_ids: list[str],
    *,
    user_id: int | None = None,
) -> dict[str, dict[str, float]]:
    """Return normalized citation graph signals keyed by paper external_id.

    The function degrades to zero/empty signals when ``networkx`` is not
    installed or when citation data is unavailable.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.
    external_ids:
        External IDs of the candidate papers to score.
    user_id:
        Caller's user ID used to scope the ``liked`` CTE so that Adamic-Adar
        scores are computed from **this user's** positive feedback only.
        When *None* (single-user / system mode), only rows with
        ``recommendation_feedback.user_id IS NULL`` are included, preserving
        the existing single-tenant behaviour.
    """
    if not external_ids:
        return {}
    try:
        import networkx as nx
    except ImportError:
        return {}

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH candidates AS (
                SELECT id FROM papers WHERE external_id = ANY($1::text[])
            ),
            liked AS (
                SELECT DISTINCT rf.paper_id AS id
                FROM recommendation_feedback rf
                WHERE rf.signal = 'positive'
                  AND rf.source IN ('pulse_thumbs', 'dismiss_combined')
                  AND rf.user_id IS NOT DISTINCT FROM $2
                ORDER BY rf.paper_id DESC
                LIMIT 100
            ),
            relevant AS (
                SELECT id FROM candidates
                UNION
                SELECT id FROM liked
            )
            SELECT p.external_id, p.id, p.citation_count,
                   (p.id IN (SELECT id FROM candidates)) AS is_candidate,
                   (p.id IN (SELECT id FROM liked)) AS is_liked,
                   pc.source_paper_id, pc.cited_paper_id
            FROM papers p
            LEFT JOIN paper_citations pc
              ON pc.source_paper_id = p.id OR pc.cited_paper_id = p.id
            WHERE p.id IN (SELECT id FROM relevant)
            """,
            external_ids,
            user_id,
        )
    if not rows:
        return {}

    graph = nx.Graph()
    id_to_external: dict[int, str] = {}
    citation_counts: dict[str, int] = {}
    liked_ids: set[int] = set()
    for row in rows:
        paper_id = int(row["id"])
        external_id = row["external_id"]
        graph.add_node(paper_id)
        if row.get("is_candidate", external_id in external_ids):
            id_to_external[paper_id] = external_id
            citation_counts[external_id] = int(row["citation_count"] or 0)
        if row.get("is_liked"):
            liked_ids.add(paper_id)
        if row["source_paper_id"] and row["cited_paper_id"]:
            graph.add_edge(int(row["source_paper_id"]), int(row["cited_paper_id"]))

    if graph.number_of_nodes() == 0:
        return {}
    try:
        pagerank = nx.pagerank(graph) if graph.number_of_edges() else {n: 0.0 for n in graph.nodes}
    except Exception:
        logger.debug("pulse citation pagerank failed", exc_info=True)
        pagerank = {n: 0.0 for n in graph.nodes}

    max_pr = max(pagerank.values(), default=0.0) or 1.0
    max_citations = max(citation_counts.values(), default=0) or 1
    adamic_adar: dict[int, float] = {paper_id: 0.0 for paper_id in id_to_external}
    if liked_ids and graph.number_of_edges():
        pairs = [
            (candidate_id, liked_id)
            for candidate_id in id_to_external
            for liked_id in liked_ids
            if candidate_id != liked_id
        ]
        try:
            for candidate_id, _liked_id, score in nx.adamic_adar_index(graph, ebunch=pairs):
                adamic_adar[candidate_id] += float(score)
        except Exception:
            logger.debug("pulse citation Adamic-Adar failed", exc_info=True)
    max_adamic = max(adamic_adar.values(), default=0.0) or 1.0
    signals: dict[str, dict[str, float]] = {}
    for paper_id, external_id in id_to_external.items():
        signals[external_id] = {
            "citation_pagerank": float(pagerank.get(paper_id, 0.0)) / max_pr,
            "citation_count": min(1.0, citation_counts.get(external_id, 0) / max_citations),
            "citation_adamic_adar": float(adamic_adar.get(paper_id, 0.0)) / max_adamic,
        }
    return signals
