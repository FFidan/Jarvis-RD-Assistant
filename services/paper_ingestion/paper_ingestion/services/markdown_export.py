"""Markdown knowledge export for a single paper (Obsidian-friendly, no HTML)."""

from __future__ import annotations

import json
import re
from typing import Any, NamedTuple

from fastapi import HTTPException

from paper_ingestion.citation_format import CitationFormat, build_citations
from paper_ingestion.db_types import ConnLike
from paper_ingestion.integrations.zotero_service import _resolve_zotero_user_id

__all__ = ["PaperMarkdown", "build_paper_markdown"]

NO_SUMMARY_PLACEHOLDER = "_This paper has not yet been summarized._"

# Every per-user read binds the caller's id as $2, mirroring the scoping shapes
# in services/data_export.py::_EXPORT_QUERIES. paper_summaries keeps the IS NOT
# DISTINCT FROM form of the paper-detail read it mirrors (routers/papers_detail.py);
# the caller id is always a concrete int here, so it behaves as strict equality
# and a summary owned by no user is not exported. notes/cards/extractions use
# strict equality, matching routers/notes.py and routers/extractions.py.
_PAPER_SQL = """
    SELECT p.*, l.zotero_citation_key AS link_citation_key
    FROM papers p
    LEFT JOIN paper_user_zotero_links l ON l.paper_id = p.id AND l.user_id = $2
    WHERE p.id = $1
"""
_SUMMARY_SQL = """
    SELECT summary_detailed, methodology, limitations
    FROM paper_summaries
    WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2
      AND content_generation = (SELECT content_generation FROM papers WHERE id = $1)
    ORDER BY created_at DESC LIMIT 1
"""
_NOTES_SQL = """
    SELECT user_note, highlight_text, page_number, content_generation
    FROM paper_notes
    WHERE paper_id = $1 AND user_id = $2
    ORDER BY created_at, id
"""
_CARDS_SQL = """
    SELECT front, back, content_generation
    FROM cards
    WHERE paper_id = $1 AND user_id = $2
    ORDER BY id
"""
_EXTRACTIONS_SQL = """
    SELECT extractions
    FROM paper_extractions
    WHERE paper_id = $1 AND user_id = $2
      AND content_generation = (SELECT content_generation FROM papers WHERE id = $1)
    ORDER BY id
"""


class PaperMarkdown(NamedTuple):
    """Rendered export plus the filename stem the download should use."""

    stem: str
    text: str


def _inline(value: Any) -> str:
    """Collapse a value to a single line so it cannot break list/table syntax."""
    return " ".join(str(value or "").split())


def _cell(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value", "")
    elif isinstance(value, list):
        value = json.dumps(value, default=str)
    return _inline(value).replace("|", "\\|")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:80]


def _front_matter(paper: dict[str, Any], citation_key: str | None) -> str:
    lines = ["---", f"title: {json.dumps(_inline(paper.get('title')))}"]
    authors = [str(a) for a in (paper.get("authors") or [])]
    if authors:
        lines.append(f"authors: {json.dumps(authors)}")
    if published := paper.get("published_date"):
        lines.append(f"date: {published}")
    if url := paper.get("url"):
        lines.append(f"url: {json.dumps(str(url))}")
    if citation_key:
        lines.append(f"citation_key: {json.dumps(citation_key)}")
    lines.append("---")
    return "\n".join(lines)


def _summary_section(row: Any) -> str:
    detailed = str(row["summary_detailed"] or "").strip() if row else ""
    if not detailed:
        return f"## Summary\n\n{NO_SUMMARY_PLACEHOLDER}"
    blocks = ["## Summary", detailed]
    for heading, column in (("Methodology", "methodology"), ("Limitations", "limitations")):
        if value := str(row[column] or "").strip():
            blocks += [f"### {heading}", value]
    return "\n\n".join(blocks)


def _notes_section(rows: list[Any], heading: str = "Notes") -> str:
    if not rows:
        return f"## {heading}\n\n_No notes._"
    items = []
    for row in rows:
        page = f" (p. {row['page_number']})" if row["page_number"] else ""
        items.append(f"- {_inline(row['user_note'])}{page}")
        if highlight := _inline(row["highlight_text"]):
            items.append(f"  > {highlight}")
    return f"## {heading}\n\n" + "\n".join(items)


def _cards_section(rows: list[Any], heading: str = "Cards") -> str:
    if not rows:
        return f"## {heading}\n\n_No cards._"
    blocks = [f"## {heading}"]
    for row in rows:
        blocks += [f"### {_inline(row['front'])}", str(row["back"] or "").strip()]
    return "\n\n".join(blocks)


def _extractions_section(rows: list[Any]) -> str:
    tables = []
    for row in rows:
        fields = row["extractions"] or {}
        if not fields:
            continue
        table = ["| Field | Value |", "| --- | --- |"]
        table += [f"| {_cell(name)} | {_cell(value)} |" for name, value in fields.items()]
        tables.append("\n".join(table))
    if not tables:
        return "## Extractions\n\n_No extractions._"
    return "## Extractions\n\n" + "\n\n".join(tables)


def _citation_section(paper: dict[str, Any]) -> str:
    bibtex = build_citations([paper], CitationFormat.BIBTEX).strip()
    return f"## Citation\n\n```bibtex\n{bibtex}\n```"


async def build_paper_markdown(
    conn: ConnLike,
    paper_id: int,
    user_id: int | None,
) -> PaperMarkdown:
    """Render one paper's summary, notes, cards, extractions, and BibTeX as Markdown.

    The caller must have established access to *paper_id*. Every workspace read
    below is additionally scoped to *user_id* so one paper record never causes
    another user's derived content to be exported.
    """
    resolved_uid = await _resolve_zotero_user_id(conn, user_id)
    paper_row = await conn.fetchrow(_PAPER_SQL, paper_id, resolved_uid)
    if paper_row is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    summary_row = await conn.fetchrow(_SUMMARY_SQL, paper_id, user_id)
    note_rows = await conn.fetch(_NOTES_SQL, paper_id, user_id)
    card_rows = await conn.fetch(_CARDS_SQL, paper_id, user_id)
    extraction_rows = await conn.fetch(_EXTRACTIONS_SQL, paper_id, user_id)

    generation = paper_row.get("content_generation")
    current_notes = [
        row for row in note_rows if row.get("content_generation") in {None, generation}
    ]
    stale_notes = [
        row for row in note_rows if row.get("content_generation") not in {None, generation}
    ]
    current_cards = [
        row for row in card_rows if row.get("content_generation") in {None, generation}
    ]
    stale_cards = [
        row for row in card_rows if row.get("content_generation") not in {None, generation}
    ]

    citation_key = paper_row["link_citation_key"]
    paper = dict(paper_row) | {"zotero_citation_key": citation_key}
    sections = [
        _front_matter(paper, citation_key),
        f"# {_inline(paper.get('title'))}",
        _summary_section(summary_row),
        _notes_section(current_notes),
        _cards_section(current_cards),
        _notes_section(stale_notes, "Notes from a previous source version") if stale_notes else "",
        _cards_section(stale_cards, "Cards from a previous source version") if stale_cards else "",
        _extractions_section(list(extraction_rows)),
        _citation_section(paper),
    ]
    stem = _slug(str(paper.get("title") or "")) or f"paper-{paper_id}"
    return PaperMarkdown(
        stem=stem,
        text="\n\n".join(section for section in sections if section) + "\n",
    )
