"""Shared loading of the quote-verification substrate.

Several surfaces (contradiction scanning, summarization, entity extraction,
template extraction, RAG answer verification) verify LLM-generated quotes
against a paper's stored text. Each needs the same substrate: the paper's
chunks in ``chunk_index`` order plus the concatenated full text. This module
is the single place that substrate is loaded from ``paper_chunks``, so the
column set and the ordering — both of which retrieval quality silently
depends on — cannot drift between surfaces.
"""

from paper_ingestion.converters import row_to_chunk_response
from paper_ingestion.db_types import ConnLike
from paper_ingestion.models import ChunkResponse

_SUBSTRATE_FROM_SQL = "FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index"

# Every column ChunkResponse carries — including created_at, so no caller has
# to invent a value for a required field the query forgot to select.
_CHUNKS_SQL = (
    "SELECT id, chunk_index, content, page_number,"
    " start_char, end_char, embedding_id, created_at, paper_id " + _SUBSTRATE_FROM_SQL
)

_TEXT_SQL = "SELECT content " + _SUBSTRATE_FROM_SQL


async def load_paper_chunks(conn: ConnLike, paper_id: int) -> list[ChunkResponse]:
    """Load a paper's stored chunks in ``chunk_index`` order.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds; this issues no acquisition of
        its own.
    paper_id : int
        Paper whose chunks are needed.

    Returns
    -------
    list[ChunkResponse]
        One entry per ``paper_chunks`` row, ordered by ``chunk_index``.
        Empty when the paper has no rows; callers keep their own policy for
        that case (raise, degrade, or skip).
    """
    rows = await conn.fetch(_CHUNKS_SQL, paper_id)
    return [row_to_chunk_response(row) for row in rows]


async def load_verification_substrate(
    conn: ConnLike, paper_id: int, *, separator: str = "\n\n"
) -> tuple[str, list[ChunkResponse]]:
    """Load the ``(full_text, chunks)`` pair quote verification runs against.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds.
    paper_id : int
        Paper whose substrate is needed.
    separator : str
        String joining chunk contents into ``full_text``. For verification
        the choice is cosmetic: ``QuoteVerifier._normalize`` collapses every
        whitespace run before matching, so ``"\\n"`` and ``"\\n\\n"`` verify
        identically. The default is ``"\\n\\n"``, the join most call sites
        already used. The parameter exists because ``full_text`` also feeds
        LLM prompt payloads at some call sites, where the separator is
        byte-visible and shifts where a length cap truncates — a call site
        with a different historic join keeps it here instead of changing its
        prompt.

    Returns
    -------
    tuple[str, list[ChunkResponse]]
        The joined full text and the chunks it was joined from, ordered by
        ``chunk_index``. ``("", [])`` when the paper has no chunks.
    """
    chunks = await load_paper_chunks(conn, paper_id)
    return separator.join(c.content for c in chunks), chunks


async def load_verification_text(conn: ConnLike, paper_id: int) -> str:
    """Load only the joined full text for *paper_id*.

    Thin variant of ``load_verification_substrate`` for callers that never
    inspect per-chunk fields: it selects only ``content`` instead of building
    ``ChunkResponse`` objects the caller would discard. Same ordering, same
    default join.
    """
    rows = await conn.fetch(_TEXT_SQL, paper_id)
    return "\n\n".join(row["content"] for row in rows)
