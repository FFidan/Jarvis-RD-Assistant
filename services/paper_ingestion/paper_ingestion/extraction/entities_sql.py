"""SQL persistence layer for knowledge-graph entities and relationships."""

import logging
from typing import Any

import asyncpg
from jarvis_common import escape_like

from paper_ingestion.db_types import ConnLike
from paper_ingestion.queries.predicates import paper_visible_sql

logger = logging.getLogger(__name__)


def _visible_paper_entities_exists(entity_alias: str, param_idx: int) -> str:
    """Build an entity-existence check over caller-visible papers.

    Parameters
    ----------
    entity_alias : str
        Trusted SQL expression identifying an entity row.
    param_idx : int
        One-based PostgreSQL placeholder containing the caller's user ID.

    Returns
    -------
    str
        An ``EXISTS`` fragment joining entity mentions to papers that are
        public or explicitly present in the caller's library.

    Notes
    -----
    Both arguments are static application SQL, never request text. The user
    recorded on ``paper_entities`` is extraction attribution, not authority.
    """
    return (
        f"EXISTS (SELECT 1 FROM paper_entities pe "
        f"JOIN papers visible_p ON visible_p.id = pe.paper_id "
        f"WHERE pe.entity_id = {entity_alias} "
        f"AND {paper_visible_sql(param_idx, alias='visible_p')})"
    )


def visible_entity_paper_count_sql(entity_alias: str, param_idx: int) -> str:
    """Build a scalar count of distinct caller-visible papers for an entity.

    Parameters
    ----------
    entity_alias : str
        Trusted SQL expression identifying an entity row.
    param_idx : int
        One-based PostgreSQL placeholder containing the caller's user ID.

    Returns
    -------
    str
        A scalar subquery counting distinct visible papers. Multiple
        extraction-attribution rows for one paper count only once.
    """
    return (
        "(SELECT COUNT(DISTINCT pe.paper_id) FROM paper_entities pe "
        "JOIN papers visible_p ON visible_p.id = pe.paper_id "
        f"WHERE pe.entity_id = {entity_alias} "
        f"AND {paper_visible_sql(param_idx, alias='visible_p')})"
    )


async def _find_or_create_entity(
    conn: ConnLike,
    name: str,
    entity_type: str,
    description: str | None,
    *,
    embedding: list[float] | None = None,
    similar_entity_id: int | None = None,
) -> tuple[int, bool]:
    """Resolve an entity id via exact-match lookup, then optional vector dedup.

    Embedding computation and Qdrant similarity search must be performed
    **before** calling this function (outside any DB connection scope) so
    that long-running HTTP calls do not hold a database connection.  Pass
    the pre-computed results via *embedding* and *similar_entity_id*.

    ``paper_count`` is **not** incremented here.  The caller
    (``extract_entities_for_paper``) increments it only when the
    ``paper_entities`` upsert actually INSERTs a new ``(paper, entity, user)``
    row (detected via ``RETURNING (xmax = 0)``), so re-running extraction for
    the same paper — or the LLM emitting the same entity name twice in one run
    — never double-counts.
    """
    canonical = name.lower().strip()

    existing = await conn.fetchrow(
        "SELECT id FROM entities WHERE canonical_name = $1 AND entity_type = $2",
        canonical,
        entity_type,
    )
    if existing:
        return existing["id"], True

    # Use pre-computed similarity result from Qdrant (computed outside conn scope)
    if similar_entity_id is not None:
        return similar_entity_id, True

    row = await conn.fetchrow(
        """INSERT INTO entities (name, canonical_name, entity_type, description)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (canonical_name, entity_type) DO NOTHING
           RETURNING id""",
        name,
        canonical,
        entity_type,
        description,
    )

    if row is None:
        # Lost a concurrent INSERT race; fetch the winner's id.
        row = await conn.fetchrow(
            "SELECT id FROM entities WHERE canonical_name = $1 AND entity_type = $2",
            canonical,
            entity_type,
        )

    if row is None:
        # Race recovery itself failed — e.g. a concurrent DELETE between the
        # INSERT-DO-NOTHING and the recovery SELECT. Surface to the caller
        # rather than crashing with a confusing NoneType subscript downstream.
        raise RuntimeError(
            f"failed to resolve entity after concurrent insert: "
            f"canonical={canonical!r} type={entity_type!r}"
        )

    entity_id: int = row["id"]

    return entity_id, False


async def get_knowledge_graph(
    conn: ConnLike,
    entity_type: str | None = None,
    min_paper_count: int = 1,
    limit: int = 200,
    user_id: int | None = None,
) -> dict:
    """Get the full knowledge graph or a filtered subset.

    When *user_id* is provided, nodes and edges are derived only from public
    papers or papers explicitly present in the caller's library. Passing
    ``None`` preserves the trusted internal path (unscoped).
    """
    try:
        if entity_type:
            if user_id is not None:
                entities = await conn.fetch(
                    f"""SELECT e.id, e.name, e.canonical_name, e.entity_type, e.description,
                              e.metadata, e.embedding_id,
                              {visible_entity_paper_count_sql("e.id", 4)} AS paper_count,
                              e.created_at
                       FROM entities e
                       WHERE e.entity_type = $1
                         AND {visible_entity_paper_count_sql("e.id", 4)} >= $2
                       ORDER BY paper_count DESC LIMIT $3""",
                    entity_type,
                    min_paper_count,
                    limit,
                    user_id,
                )
            else:
                entities = await conn.fetch(
                    """SELECT id, name, canonical_name, entity_type, description, metadata,
                              embedding_id, paper_count, created_at FROM entities
                       WHERE entity_type = $1 AND paper_count >= $2
                       ORDER BY paper_count DESC LIMIT $3""",
                    entity_type,
                    min_paper_count,
                    limit,
                )
        else:
            if user_id is not None:
                entities = await conn.fetch(
                    f"""SELECT e.id, e.name, e.canonical_name, e.entity_type, e.description,
                              e.metadata, e.embedding_id,
                              {visible_entity_paper_count_sql("e.id", 3)} AS paper_count,
                              e.created_at
                       FROM entities e
                       WHERE {visible_entity_paper_count_sql("e.id", 3)} >= $1
                       ORDER BY paper_count DESC LIMIT $2""",
                    min_paper_count,
                    limit,
                    user_id,
                )
            else:
                entities = await conn.fetch(
                    """SELECT id, name, canonical_name, entity_type, description, metadata,
                              embedding_id, paper_count, created_at FROM entities
                       WHERE paper_count >= $1
                       ORDER BY paper_count DESC LIMIT $2""",
                    min_paper_count,
                    limit,
                )
    except asyncpg.exceptions.UndefinedTableError:
        return {"entities": [], "relationships": []}

    entity_ids = [e["id"] for e in entities]
    if not entity_ids:
        return {"entities": [], "relationships": []}

    if user_id is not None:
        # An authenticated edge must retain a caller-visible source paper.
        # NULL paper references fail closed because their prior visibility can
        # no longer be established after the source row was deleted.
        relationships = await conn.fetch(
            f"""SELECT id, source_entity_id, target_entity_id, relationship_type,
                      paper_id, evidence_quote, confidence, metadata, created_at
               FROM entity_relationships er
               WHERE source_entity_id = ANY($1) AND target_entity_id = ANY($1)
                 AND EXISTS (
                     SELECT 1 FROM papers p
                     WHERE p.id = er.paper_id
                       AND {paper_visible_sql(2)}
                 )
               ORDER BY confidence DESC""",
            entity_ids,
            user_id,
        )
    else:
        relationships = await conn.fetch(
            """SELECT id, source_entity_id, target_entity_id, relationship_type,
                      paper_id, evidence_quote, confidence, metadata, created_at
               FROM entity_relationships
               WHERE source_entity_id = ANY($1) AND target_entity_id = ANY($1)
               ORDER BY confidence DESC""",
            entity_ids,
        )

    entity_dicts: list[dict[str, Any]] = []
    entity_type_counts: dict[str, int] = {}
    for e in entities:
        paper_count = e["paper_count"]
        etype = e["entity_type"]
        entity_type_counts[etype] = entity_type_counts.get(etype, 0) + 1
        entity_dicts.append(
            {
                "id": e["id"],
                "name": e["name"],
                "canonical_name": e.get("canonical_name"),
                "entity_type": etype,
                "description": e.get("description"),
                "metadata": e.get("metadata"),
                "embedding_id": e.get("embedding_id"),
                "paper_count": paper_count,
                "created_at": e.get("created_at"),
                "display_size": min(40, max(15, 15 + paper_count * 3)),
            }
        )

    return {
        "entities": entity_dicts,
        "relationships": [dict(r) for r in relationships],
        "entity_type_counts": entity_type_counts,
    }


async def query_knowledge_graph(
    conn: ConnLike,
    query: str,
    user_id: int | None = None,
) -> list[dict]:
    """Answer a knowledge graph query using SQL pattern matching on entities.

    Dispatch table (each branch has scoped/unscoped SQL variants on ``user_id``):
      1. "used on" | "applied to" → relationship search filtered by target entity name
      2. "outperforms" | "better than" → outperformance relationship search
      3. else → generic name LIKE search across entities joined to paper_entities

    When *user_id* is provided, results are restricted through the papers'
    persisted visibility and caller-library membership. Passing ``None``
    preserves the trusted internal path (unscoped).
    """
    # Simple keyword extraction for SQL matching
    query_lower = query.lower()

    # Detect query pattern
    try:
        if "used on" in query_lower or "applied to" in query_lower:
            # "What methods are used on dataset X?"
            # Extract the target entity name
            target_name = ""
            for keyword in ["used on", "applied to"]:
                if keyword in query_lower:
                    target_name = query_lower.split(keyword)[-1].strip().rstrip("?. ")
                    break

            if user_id is not None:
                rows = await conn.fetch(
                    f"""SELECT e1.name AS method_name, e1.entity_type AS method_type,
                              e2.name AS target_name, e2.entity_type AS target_type,
                              er.relationship_type, er.evidence_quote, er.confidence
                       FROM entity_relationships er
                       JOIN entities e1 ON er.source_entity_id = e1.id
                       JOIN entities e2 ON er.target_entity_id = e2.id
                       WHERE LOWER(e2.name) LIKE $1 ESCAPE '\\'
                         AND er.relationship_type IN ('used_on', 'evaluates', 'applied_to')
                         AND {_visible_paper_entities_exists("e1.id", 2)}
                         AND EXISTS (
                             SELECT 1 FROM papers p
                             WHERE p.id = er.paper_id
                               AND {paper_visible_sql(2)}
                         )
                       ORDER BY er.confidence DESC""",
                    f"%{escape_like(target_name)}%",
                    user_id,
                )
            else:
                rows = await conn.fetch(
                    """SELECT e1.name AS method_name, e1.entity_type AS method_type,
                              e2.name AS target_name, e2.entity_type AS target_type,
                              er.relationship_type, er.evidence_quote, er.confidence
                       FROM entity_relationships er
                       JOIN entities e1 ON er.source_entity_id = e1.id
                       JOIN entities e2 ON er.target_entity_id = e2.id
                       WHERE LOWER(e2.name) LIKE $1 ESCAPE '\\'
                         AND er.relationship_type IN ('used_on', 'evaluates', 'applied_to')
                       ORDER BY er.confidence DESC""",
                    f"%{escape_like(target_name)}%",
                )
            return [dict(r) for r in rows]

        elif "outperforms" in query_lower or "better than" in query_lower:
            if user_id is not None:
                rows = await conn.fetch(
                    f"""SELECT e1.name AS method_name, e2.name AS compared_to,
                              er.evidence_quote, er.confidence
                       FROM entity_relationships er
                       JOIN entities e1 ON er.source_entity_id = e1.id
                       JOIN entities e2 ON er.target_entity_id = e2.id
                       WHERE er.relationship_type = 'outperforms'
                         AND {_visible_paper_entities_exists("e1.id", 1)}
                         AND EXISTS (
                             SELECT 1 FROM papers p
                             WHERE p.id = er.paper_id
                               AND {paper_visible_sql(1)}
                         )
                       ORDER BY er.confidence DESC
                       LIMIT 50""",
                    user_id,
                )
            else:
                rows = await conn.fetch(
                    """SELECT e1.name AS method_name, e2.name AS compared_to,
                              er.evidence_quote, er.confidence
                       FROM entity_relationships er
                       JOIN entities e1 ON er.source_entity_id = e1.id
                       JOIN entities e2 ON er.target_entity_id = e2.id
                       WHERE er.relationship_type = 'outperforms'
                       ORDER BY er.confidence DESC
                       LIMIT 50""",
                )
            return [dict(r) for r in rows]

        else:
            # Generic: search entities by name
            if user_id is not None:
                rows = await conn.fetch(
                    f"""SELECT DISTINCT e.*, pe.paper_id, p.title AS paper_title
                       FROM entities e
                       JOIN paper_entities pe ON e.id = pe.entity_id
                       JOIN papers p ON p.id = pe.paper_id
                       WHERE LOWER(e.name) LIKE $1 ESCAPE '\\'
                         AND {paper_visible_sql(2)}
                       ORDER BY e.paper_count DESC
                       LIMIT 20""",
                    f"%{escape_like(query_lower.strip().rstrip('?. '))}%",
                    user_id,
                )
            else:
                rows = await conn.fetch(
                    """SELECT e.*, pe.paper_id,
                              (SELECT title FROM papers p WHERE p.id = pe.paper_id) AS paper_title
                       FROM entities e
                       JOIN paper_entities pe ON e.id = pe.entity_id
                       WHERE LOWER(e.name) LIKE $1 ESCAPE '\\'
                       ORDER BY e.paper_count DESC
                       LIMIT 20""",
                    f"%{escape_like(query_lower.strip().rstrip('?. '))}%",
                )
            return [dict(r) for r in rows]
    except asyncpg.exceptions.UndefinedTableError:
        return []
