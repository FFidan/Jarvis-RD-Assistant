"""Stored-chunk backing for retrieved excerpts.

Retrieved excerpts carry their own copy of the text in the vector payload, so a
surface that serves that text checks it against the paper's current stored chunk
records here rather than re-reading the text from SQL.
"""

import logging

from paper_ingestion.db_types import ConnLike

logger = logging.getLogger(__name__)

_STORED_CHUNK_KEYS_SQL = (
    "SELECT paper_id, chunk_index FROM paper_chunks WHERE paper_id = ANY($1::int[])"
)


async def read_stored_chunk_keys(conn: ConnLike, paper_ids: list[int]) -> set[tuple[int, int]]:
    """Read the ``(paper_id, chunk_index)`` pairs currently stored for these papers.

    Parameters
    ----------
    conn : ConnLike
        Connection the caller already holds; this issues no acquisition of its own.
    paper_ids : list[int]
        Papers whose stored chunk records are needed.

    Returns
    -------
    set[tuple[int, int]]
        One pair per row in ``paper_chunks``. Empty when a paper has no rows.
    """
    rows = await conn.fetch(_STORED_CHUNK_KEYS_SQL, paper_ids)
    return {(row["paper_id"], row["chunk_index"]) for row in rows}


def drop_chunks_without_stored_rows(
    chunks: list[dict],
    stored_keys: set[tuple[int, int]],
    *,
    paper_id: int | None = None,
    caller: str,
    level: int = logging.WARNING,
) -> list[dict]:
    """Keep only retrieved chunks backed by a stored ``paper_chunks`` record.

    A retrieved chunk carries its own copy of the excerpt text in the vector
    payload, so nothing downstream re-reads the text from SQL. ``paper_chunks``
    is the authoritative record of a paper's stored text: when a paper is
    re-processed or its derived content is discarded, those rows are rewritten
    or deleted while the vector points are removed separately. Requiring the row
    bounds what may be served to the chunks the paper currently stores; the
    check compares keys and does not compare the payload text to the row.

    Parameters
    ----------
    chunks : list[dict]
        Retrieved chunks, each carrying ``chunk_index`` and — for cross-paper
        retrieval — ``paper_id``.
    stored_keys : set[tuple[int, int]]
        Pairs read from ``paper_chunks`` covering every paper in ``chunks``.
    paper_id : int or None
        Owning paper for single-paper retrieval, whose chunks carry no
        ``paper_id`` of their own. ``None`` takes the owner from each chunk.
    caller : str
        Name of the retrieval surface, recorded so a drop can be attributed.
    level : int
        Level a partial drop is logged at. Dropping means different things to an
        operator per surface, so each caller states its own: on a surface where
        a drop reports that a paper's content was superseded the default
        ``WARNING`` is the signal, while on one that drops as an ordinary part
        of answering a request a lower level keeps that signal readable. Losing
        the whole retrieval is a different event from filtering part of it — the
        surface answers empty and succeeds — so it is never reported below
        ``WARNING`` whatever the caller asked for.

    Returns
    -------
    list[dict]
        The backed subset, in input order. ``chunk_index`` is NOT NULL in
        ``paper_chunks``, so a chunk carrying none matches nothing and is dropped.
    """
    kept: list[dict] = []
    dropped_paper_ids: set[int] = set()
    for chunk in chunks:
        owner = paper_id if paper_id is not None else chunk.get("paper_id")
        if (owner, chunk.get("chunk_index")) in stored_keys:
            kept.append(chunk)
        elif owner is not None:
            dropped_paper_ids.add(owner)
    dropped = len(chunks) - len(kept)
    if dropped:
        logger.log(
            level if kept else max(level, logging.WARNING),
            "%s dropped %d of %d retrieved chunks with no stored chunk record (papers: %s)",
            caller,
            dropped,
            len(chunks),
            sorted(dropped_paper_ids),
        )
    return kept
