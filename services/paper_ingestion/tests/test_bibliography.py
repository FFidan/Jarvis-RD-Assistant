"""Tests for uploaded-paper identification and bibliography retention."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn

from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.services.bibliography import (
    BibliographyExtraction,
    ParsedBibliographyEntry,
    extract_document_identifiers,
    identify_uploaded_document,
    is_confident_title_match,
    parse_bibliography_entries,
    process_uploaded_document_citations,
    split_bibliography_entries,
    title_match_score,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _paper(
    *,
    paper_id: str,
    title: str,
    year: int,
    doi: str | None = None,
    arxiv_id: str | None = None,
) -> PaperCreate:
    metadata = {"s2_id": paper_id}
    if doi:
        metadata["doi"] = doi
    if arxiv_id:
        metadata["arxiv_id"] = arxiv_id
    return PaperCreate(
        external_id=f"s2:{paper_id}",
        source_type=SourceType.SEMANTIC_SCHOLAR,
        title=title,
        authors=["Researcher, A."],
        abstract=None,
        published_date=date(year, 1, 1),
        url=f"https://www.semanticscholar.org/paper/{paper_id}",
        metadata=metadata,
    )


def test_document_identifiers_are_extracted_from_realistic_front_matter() -> None:
    text = (FIXTURES / "uploaded_identifiers.txt").read_text()

    identifiers = extract_document_identifiers(text)

    assert identifiers.doi == "10.5555/12345678.2024.9"
    assert identifiers.arxiv_id == "2403.01234v2"


def test_title_resolution_requires_exact_normalized_title_and_year() -> None:
    exact = _paper(
        paper_id="exact",
        title="Precision First: Document-Linking",
        year=2024,
    )
    near_miss = _paper(
        paper_id="near-miss",
        title="Precision First Document Linking for Libraries",
        year=2024,
    )

    assert title_match_score("Precision first document linking", exact.title) == 1.0
    assert is_confident_title_match("Precision first document linking", 2024, exact)
    assert title_match_score("Precision first document linking", near_miss.title) < 1.0
    assert not is_confident_title_match("Precision first document linking", 2024, near_miss)
    assert not is_confident_title_match("Precision first document linking", 2023, exact)


@pytest.mark.parametrize(
    ("fixture_name", "expected_count", "expected_fragments"),
    [
        (
            "bibliography_numbered.md",
            3,
            ["Attention Is All You Need", "laboratory indexing", "Evidence-preserving"],
        ),
        (
            "bibliography_author_year_two_column.md",
            4,
            ["Conservative matching", "Local documents", "Citation graphs", "Unindexed"],
        ),
    ],
)
def test_real_bibliography_shapes_split_without_dropping_entries(
    fixture_name: str,
    expected_count: int,
    expected_fragments: list[str],
) -> None:
    markdown = (FIXTURES / fixture_name).read_text()

    entries = split_bibliography_entries(markdown)

    assert len(entries) == expected_count
    assert all(fragment in entry for fragment, entry in zip(expected_fragments, entries))
    assert all("Appendix" not in entry for entry in entries)


@pytest.mark.asyncio
async def test_structured_bibliography_parsing_preserves_explicit_identifiers() -> None:
    raw_entries = split_bibliography_entries((FIXTURES / "bibliography_numbered.md").read_text())
    model_response = BibliographyExtraction(
        entries=[
            ParsedBibliographyEntry(
                index=0,
                title="Attention Is All You Need",
                authors=["Vaswani, A.", "Shazeer, N."],
                year=2017,
                venue="Advances in Neural Information Processing Systems 30",
                doi="10.0000/model-invented",
            ),
            ParsedBibliographyEntry(
                index=1,
                title="Notes on laboratory indexing",
                authors=["Rivera, M."],
                year=2021,
                venue="Institute Technical Report",
            ),
            ParsedBibliographyEntry(
                index=2,
                title="Evidence-preserving search systems",
                authors=["Smith, J."],
                year=2020,
                venue="Journal of Research Tools 8",
            ),
        ]
    )

    with patch(
        "paper_ingestion.services.bibliography.call_llm_structured",
        AsyncMock(return_value=model_response),
    ):
        parsed = await parse_bibliography_entries(raw_entries, MagicMock())

    assert [(entry.title, entry.year, entry.venue) for entry in parsed] == [
        (
            "Attention Is All You Need",
            2017,
            "Advances in Neural Information Processing Systems 30",
        ),
        ("Notes on laboratory indexing", 2021, "Institute Technical Report"),
        ("Evidence-preserving search systems", 2020, "Journal of Research Tools 8"),
    ]
    assert parsed[0].authors == ["Vaswani, A.", "Shazeer, N."]
    assert parsed[0].doi is None
    assert parsed[0].arxiv_id == "1706.03762"
    assert parsed[2].doi == "10.5555/example.2020.8"


@pytest.mark.asyncio
async def test_two_column_author_year_entries_parse_into_structured_fields() -> None:
    raw_entries = split_bibliography_entries(
        (FIXTURES / "bibliography_author_year_two_column.md").read_text()
    )
    expected = [
        ("Conservative matching for scholarly records", 2019, "Journal of Metadata 4"),
        ("Local documents in research systems", 2022, "Workshop on Personal Knowledge Bases"),
        (
            "Citation graphs without fabricated edges",
            2020,
            "Proceedings of Reliable Information Retrieval",
        ),
        ("Unindexed manuscripts and honest interfaces", 2023, "Research Notes"),
    ]
    model_response = BibliographyExtraction(
        entries=[
            ParsedBibliographyEntry(index=index, title=title, year=year, venue=venue)
            for index, (title, year, venue) in enumerate(expected)
        ]
    )

    with patch(
        "paper_ingestion.services.bibliography.call_llm_structured",
        AsyncMock(return_value=model_response),
    ):
        parsed = await parse_bibliography_entries(raw_entries, MagicMock())

    assert [(entry.title, entry.year, entry.venue) for entry in parsed] == expected


@pytest.mark.asyncio
async def test_structured_output_omission_keeps_an_unresolved_placeholder() -> None:
    raw_entries = ["Smith, J. (2020). First paper.", "Rivera, M. (2021). Second paper."]
    model_response = BibliographyExtraction(
        entries=[ParsedBibliographyEntry(index=0, title="First paper", year=2020)]
    )

    with patch(
        "paper_ingestion.services.bibliography.call_llm_structured",
        AsyncMock(return_value=model_response),
    ):
        parsed = await parse_bibliography_entries(raw_entries, MagicMock())

    assert len(parsed) == 2
    assert parsed[0].title == "First paper"
    assert parsed[1] == ParsedBibliographyEntry(index=1)


@pytest.mark.asyncio
async def test_identifier_inside_references_does_not_identify_the_upload() -> None:
    source = MagicMock()
    source.fetch_by_id = AsyncMock()
    source.search = AsyncMock()
    first_pages = """# An unindexed note

## References

[1] Indexed paper. https://doi.org/10.5555/reference.2024.1
"""

    identified = await identify_uploaded_document(
        source,
        title="An unindexed note",
        year=None,
        first_pages=first_pages,
    )

    assert identified is None
    source.fetch_by_id.assert_not_awaited()
    source.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_identified_upload_uses_existing_citation_refresh() -> None:
    paper_row = {
        "id": 7,
        "external_id": "local:uploaded-paper.pdf",
        "source_type": "local",
        "title": "Precision-first document linking for scientific libraries",
        "published_date": date(2024, 1, 1),
        "metadata": {},
    }
    first_pages = (FIXTURES / "uploaded_identifiers.txt").read_text()
    pool, conn = make_pool_and_conn(
        fetchrow_return=paper_row,
        fetch_return=[{"content": first_pages, "page_number": 1}],
        with_transaction=False,
    )
    source = MagicMock()
    source.fetch_by_id = AsyncMock(
        return_value=_paper(
            paper_id="canonical-paper",
            title=paper_row["title"],
            year=2024,
            doi="10.5555/12345678.2024.9",
            arxiv_id="2403.01234",
        )
    )
    source.search = AsyncMock(return_value=[])

    with patch(
        "paper_ingestion.services.bibliography._refresh_stale_citations",
        AsyncMock(),
    ) as refresh:
        await process_uploaded_document_citations(
            pool,
            7,
            s2_source=source,
            openai_client=MagicMock(),
        )

    source.fetch_by_id.assert_awaited_once_with("DOI:10.5555/12345678.2024.9")
    refresh.assert_awaited_once_with(pool, source, [7])
    update_args = conn.execute.await_args.args
    assert update_args[1] == "s2:canonical-paper"
    assert isinstance(update_args[2], Mapping), "identified metadata must be bound as a mapping"
    assert update_args[2]["s2_id"] == "canonical-paper"
    assert update_args[3:] == (7, "local:uploaded-paper.pdf")


@pytest.mark.asyncio
async def test_resolved_entries_become_edges_and_unresolved_entries_persist() -> None:
    markdown = (FIXTURES / "bibliography_numbered.md").read_text()
    paper_row = {
        "id": 9,
        "external_id": "local:unindexed.pdf",
        "source_type": "local",
        "title": "An Unindexed Manuscript",
        "published_date": None,
        "metadata": {},
    }
    pool, conn = make_pool_and_conn(
        fetchrow_return=paper_row,
        fetch_return=[{"content": markdown, "page_number": 8}],
        with_transaction=False,
    )
    parsed = [
        ParsedBibliographyEntry(
            index=0,
            title="Attention Is All You Need",
            authors=["Vaswani, A."],
            year=2017,
            arxiv_id="1706.03762",
        ),
        ParsedBibliographyEntry(
            index=1,
            title="Notes on laboratory indexing",
            authors=["Rivera, M."],
            year=2021,
            venue="Institute Technical Report",
        ),
        ParsedBibliographyEntry(
            index=2,
            title="Evidence-preserving search systems",
            authors=["Smith, J."],
            year=2020,
            doi="10.5555/example.2020.8",
        ),
    ]
    resolved = _paper(
        paper_id="attention",
        title="Attention Is All You Need",
        year=2017,
        arxiv_id="1706.03762",
    )
    source = MagicMock()
    source.fetch_by_id = AsyncMock(
        side_effect=lambda identifier: resolved if identifier == "ARXIV:1706.03762" else None
    )
    source.search = AsyncMock(return_value=[])

    with (
        patch(
            "paper_ingestion.services.bibliography.parse_bibliography_entries",
            AsyncMock(return_value=parsed),
        ),
        patch(
            "paper_ingestion.services.bibliography.sync_bibliography_references",
            AsyncMock(return_value=(1, 1)),
        ) as sync_references,
    ):
        await process_uploaded_document_citations(
            pool,
            9,
            s2_source=source,
            openai_client=MagicMock(),
        )

    sync_references.assert_awaited_once()
    assert sync_references.await_args.args[2] == 9
    assert sync_references.await_args.args[1][0]["title"] == "Attention Is All You Need"
    bound_metadata = conn.execute.await_args.args[1]
    assert isinstance(bound_metadata, Mapping), "bibliography metadata must be bound as a mapping"
    stored = bound_metadata["bibliography"]
    assert [entry["resolved"] for entry in stored] == [True, False, False]
    assert stored[1]["raw_text"].startswith("[2] Rivera")
    assert stored[1]["title"] == "Notes on laboratory indexing"


async def test_front_matter_identifier_of_another_work_does_not_claim_the_upload() -> None:
    """A DOI printed in front matter is not proof of the document's own identity.

    Journals and preprint templates routinely print a "cite this as" line or a
    footnote referring to other work, and that text sits before any References
    heading. Confirming the identifier round-tripped only proves the index agrees
    with itself, so the resolved paper's title has to agree with the upload's or
    the upload keeps its own identity.
    """
    source = MagicMock()
    source.search = AsyncMock()
    source.fetch_by_id = AsyncMock(
        return_value=_paper(
            paper_id="other-work",
            title="A completely different published paper",
            year=2024,
            doi="10.5555/12345678.2024.9",
        )
    )
    first_pages = """# Notes on an unpublished experiment

    Related discussion appears in an earlier study, doi:10.5555/12345678.2024.9.
    """

    identified = await identify_uploaded_document(
        source,
        title="Notes on an unpublished experiment",
        year=2025,
        first_pages=first_pages,
    )

    assert identified is None, (
        "an identifier belonging to another work must not rewrite this document's identity"
    )
    source.fetch_by_id.assert_awaited()


@pytest.mark.asyncio
async def test_a_failed_extraction_keeps_every_entry_as_plain_text() -> None:
    """A model failure must degrade to plain-text rows, never drop references.

    Dropping an entry would quietly shorten a paper's bibliography, which is a
    worse outcome than showing the raw text a reader can still act on.
    """
    raw_entries = split_bibliography_entries((FIXTURES / "bibliography_numbered.md").read_text())

    with patch(
        "paper_ingestion.services.bibliography.call_llm_structured",
        AsyncMock(side_effect=RuntimeError("model unavailable")),
    ):
        parsed = await parse_bibliography_entries(raw_entries, MagicMock())

    assert len(parsed) == len(raw_entries), "every reference survives a failed extraction"
    assert all(entry.title is None for entry in parsed), "no title is invented on failure"
    assert [entry.index for entry in parsed] == list(range(len(raw_entries))), (
        "every entry keeps its position, so the raw text can still be shown beside it"
    )
