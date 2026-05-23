"""GDPR data export: build a ZIP of all structured user data."""

import io
import json
import zipfile

import asyncpg

__all__ = [
    "_EXPORT_QUERIES",
    "build_export_zip",
]

# (table-in-zip name, SQL). Each query is scoped to the calling user via $1.
# Structured data only: no PDF binaries, no embeddings (paper_chunks.embedding
# / vectors are excluded — papers carries metadata + abstract, notes carry the
# user's own annotations). papers is scoped by discovered_by (canonical-corpus
# owner column, mig 072); everything else by user_id.
_EXPORT_QUERIES: tuple[tuple[str, str], ...] = (
    ("papers", "SELECT row_to_json(p) FROM papers p WHERE p.discovered_by = $1"),
    ("paper_notes", "SELECT row_to_json(t) FROM paper_notes t WHERE t.user_id = $1"),
    ("paper_summaries", "SELECT row_to_json(t) FROM paper_summaries t WHERE t.user_id = $1"),
    ("cards", "SELECT row_to_json(t) FROM cards t WHERE t.user_id = $1"),
    ("decks", "SELECT row_to_json(t) FROM decks t WHERE t.user_id = $1"),
    ("review_logs", "SELECT row_to_json(t) FROM review_logs t WHERE t.user_id = $1"),
    ("projects", "SELECT row_to_json(t) FROM projects t WHERE t.user_id = $1"),
    ("tasks", "SELECT row_to_json(t) FROM tasks t WHERE t.user_id = $1"),
    ("milestones", "SELECT row_to_json(t) FROM milestones t WHERE t.user_id = $1"),
    ("journal_entries", "SELECT row_to_json(t) FROM journal_entries t WHERE t.user_id = $1"),
    ("daily_log", "SELECT row_to_json(t) FROM daily_log t WHERE t.user_id = $1"),
    ("user_config", "SELECT row_to_json(t) FROM user_config t WHERE t.user_id = $1"),
)


async def build_export_zip(pool: asyncpg.Pool, user_id: int | None) -> bytes:
    """Build a ZIP of all user data and return the raw bytes.

    Iterates each :data:`_EXPORT_QUERIES` table inside a single read transaction
    so the snapshot is consistent.  Memory stays bounded because each table's
    rows are fetched via an asyncpg cursor.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        async with pool.acquire() as conn, conn.transaction():
            for name, sql in _EXPORT_QUERIES:
                lines: list[str] = []
                async for record in conn.cursor(sql, user_id):
                    value = record[0]
                    if isinstance(value, str):
                        lines.append(value)
                    else:
                        lines.append(json.dumps(value, default=str))
                zf.writestr(f"{name}.jsonl", "\n".join(lines))
    buf.seek(0)
    return buf.getvalue()
