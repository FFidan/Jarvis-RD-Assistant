"""Pure citation formatters: render a paper row as BibTeX or RIS text.

No I/O and no FastAPI dependencies — the router owns DB access and tenancy.
A "paper" is any mapping with ``papers``-table column keys (an asyncpg
``Record`` or a plain dict); columns are read by key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from enum import Enum
from typing import Any

import rispy
from pydantic import BaseModel

RIS_JOURNAL_ARTICLE = "JOUR"

# Paper metadata comes from external source APIs and can contain characters
# that corrupt the serialized output, or non-string JSONB values. bibtexparser's
# writer/parser is brace-counting (not LaTeX-aware): an unbalanced "{"/"}" in a
# value truncates the field (or lets a crafted title inject a sibling field), and
# a value *ending* in a backslash escapes the field-closing brace, silently
# dropping the whole entry. So drop braces *and* backslashes from BibTeX values;
# collapse newlines in RIS values (each RIS field is a single line, so an embedded
# newline would split the record). Both coerce to str first — a JSONB metadata
# value (doi/journal/venue) is not guaranteed to be a string.
_BIBTEX_KEY_DISALLOWED = re.compile(r"[^A-Za-z0-9_:.\-]")


def _bibtex_value(value: object) -> str:
    return str(value).replace("\\", "").replace("{", "").replace("}", "")


def _bibtex_key(raw: str, paper_id: Any) -> str:
    return _BIBTEX_KEY_DISALLOWED.sub("", raw) or f"paper-{paper_id}"


def _ris_value(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


class CitationFormat(str, Enum):  # noqa: UP042 — keep str+Enum for Pydantic v2 / FastAPI query coercion
    BIBTEX = "bibtex"
    RIS = "ris"


class CitationBulkRequest(BaseModel):
    paper_ids: list[int]
    format: CitationFormat = CitationFormat.BIBTEX


def content_type(fmt: CitationFormat) -> str:
    """Return the HTTP Content-Type for *fmt*."""
    return (
        "application/x-bibtex"
        if fmt is CitationFormat.BIBTEX
        else ("application/x-research-info-systems")
    )


def file_extension(fmt: CitationFormat) -> str:
    """Return the download file extension (no dot) for *fmt*."""
    return "bib" if fmt is CitationFormat.BIBTEX else "ris"


def _metadata(paper: Mapping[str, Any]) -> Mapping[str, Any]:
    # asyncpg decodes the jsonb column to a dict already; never re-parse it.
    return paper.get("metadata") or {}


def _year(paper: Mapping[str, Any]) -> str | None:
    published = paper.get("published_date")
    return str(published.year) if isinstance(published, date) else None


def _citation_key(paper: Mapping[str, Any]) -> str:
    return paper.get("zotero_citation_key") or f"paper-{paper['id']}"


def build_bibtex(paper: Mapping[str, Any]) -> str:
    """Render a single paper as one BibTeX ``@article`` entry.

    Hand-written (no BibTeX library at runtime) — the format is trivial and
    values are already brace-sanitized by ``_bibtex_value``; the tests parse the
    output back with bibtexparser (a dev-only dep) as the correctness oracle.
    """
    meta = _metadata(paper)
    fields: list[tuple[str, str]] = [("title", _bibtex_value(paper["title"]))]

    authors = paper.get("authors") or []
    if authors:
        fields.append(("author", _bibtex_value(" and ".join(authors))))
    if (year := _year(paper)) is not None:
        fields.append(("year", year))
    if doi := meta.get("doi"):
        fields.append(("doi", _bibtex_value(doi)))
    if journal := (meta.get("journal") or meta.get("venue")):
        fields.append(("journal", _bibtex_value(journal)))
    if url := paper.get("url"):
        fields.append(("url", _bibtex_value(url)))
    if abstract := paper.get("abstract"):
        fields.append(("abstract", _bibtex_value(abstract)))

    key = _bibtex_key(_citation_key(paper), paper["id"])
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@article{{{key},\n{body}\n}}\n"


def build_ris(paper: Mapping[str, Any]) -> str:
    """Render a single paper as one RIS journal-article record."""
    meta = _metadata(paper)
    ref: dict[str, Any] = {
        "type_of_reference": RIS_JOURNAL_ARTICLE,
        "id": _ris_value(_citation_key(paper)),
        "title": _ris_value(paper["title"]),
    }

    authors = paper.get("authors") or []
    if authors:
        ref["authors"] = [_ris_value(a) for a in authors]
    if (year := _year(paper)) is not None:
        ref["year"] = year
    if doi := meta.get("doi"):
        ref["doi"] = _ris_value(doi)
    if journal := (meta.get("journal") or meta.get("venue")):
        ref["journal_name"] = _ris_value(journal)
    if url := paper.get("url"):
        ref["urls"] = [_ris_value(url)]
    if abstract := paper.get("abstract"):
        ref["abstract"] = _ris_value(abstract)

    return rispy.dumps([ref])


def build_citations(papers: Sequence[Mapping[str, Any]], fmt: CitationFormat) -> str:
    """Render *papers* as concatenated citation text, one entry per paper."""
    builder = build_bibtex if fmt is CitationFormat.BIBTEX else build_ris
    return "\n".join(builder(paper) for paper in papers)
