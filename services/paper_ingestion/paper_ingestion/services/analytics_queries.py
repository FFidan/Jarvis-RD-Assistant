"""Analytics queries: paper counts grouped by source type and user-state status."""

from typing import Any

__all__ = [
    "fetch_papers_by_source",
    "fetch_papers_by_status",
]


async def fetch_papers_by_source(
    conn: Any,
    user_id: int | None,
    *,
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Return paper counts grouped by source type, scoped to the caller."""
    if user_id is None or is_admin:
        rows = await conn.fetch(
            "SELECT source_type, COUNT(*) AS count"
            " FROM papers GROUP BY source_type ORDER BY count DESC"
        )
    else:
        rows = await conn.fetch(
            """
            SELECT p.source_type, COUNT(*) AS count
            FROM papers p
            JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
            GROUP BY p.source_type
            ORDER BY count DESC
            """,
            user_id,
        )
    return [{"source_type": r["source_type"], "count": r["count"]} for r in rows]


async def fetch_papers_by_status(
    conn: Any,
    user_id: int | None,
    *,
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Return paper counts grouped by user-state status, scoped to the caller."""
    if user_id is None or is_admin:
        rows = await conn.fetch(
            """
            SELECT COALESCE(pus.state::TEXT, 'inbox') AS status, COUNT(DISTINCT p.id) AS count
            FROM papers p
            LEFT JOIN paper_user_state pus ON p.id = pus.paper_id
            GROUP BY COALESCE(pus.state::TEXT, 'inbox')
            ORDER BY count DESC
            """
        )
    else:
        rows = await conn.fetch(
            """
            SELECT COALESCE(pus.state::TEXT, 'inbox') AS status, COUNT(*) AS count
            FROM papers p
            JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
            LEFT JOIN paper_user_state pus
              ON p.id = pus.paper_id AND pus.user_id = $1
            GROUP BY COALESCE(pus.state::TEXT, 'inbox')
            ORDER BY count DESC
            """,
            user_id,
        )
    return [{"status": r["status"], "count": r["count"]} for r in rows]
