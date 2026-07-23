"""Citation-export tests: pure formatters (round-trip) + endpoint contract.

The pure tests assert the BibTeX/RIS output parses back to the expected
fields (round-trip is the contract's core acceptance). The endpoint tests
follow the live-ASGI shape from test_citations_user_scope.py: mocked asyncpg
pool/conn + dependency overrides for auth + db pool.
"""

from __future__ import annotations

import re
from datetime import date
from unittest.mock import AsyncMock

import bibtexparser
import pytest
import rispy

from jarvis_common.testing import make_pool_and_conn
from tests.conftest import FakeRecord


def _paper(
    *,
    paper_id: int = 1,
    title: str = "Attention Is All You Need",
    authors: list[str] | None = None,
    published: date | None = date(2017, 6, 12),
    url: str = "https://example.test/paper",
    metadata: dict | None = None,
    zotero_citation_key: str | None = None,
) -> FakeRecord:
    return FakeRecord(
        id=paper_id,
        title=title,
        authors=authors if authors is not None else ["Ashish Vaswani", "Noam Shazeer"],
        abstract="An abstract.",
        published_date=published,
        url=url,
        metadata=metadata if metadata is not None else {"doi": "10.1/x", "journal": "NeurIPS"},
        zotero_citation_key=zotero_citation_key,
        # Per-user citation key the get_paper_citation JOIN aliases from
        # paper_user_zotero_links; mirror zotero_citation_key so the endpoint
        # tests' stem expectations are unchanged.
        link_citation_key=zotero_citation_key,
    )


# ---------------------------------------------------------------------------
# Pure formatter round-trips
# ---------------------------------------------------------------------------


def test_build_bibtex_round_trips() -> None:
    from paper_ingestion.citation_format import build_bibtex

    text = build_bibtex(_paper(zotero_citation_key="Vaswani2017"))
    library = bibtexparser.parse_string(text)

    assert len(library.entries) == 1
    entry = library.entries[0]
    assert entry.key == "Vaswani2017"
    fields = entry.fields_dict
    assert fields["author"].value == "Ashish Vaswani and Noam Shazeer"
    assert fields["title"].value == "Attention Is All You Need"
    assert fields["doi"].value == "10.1/x"
    assert fields["year"].value == "2017"
    assert fields["journal"].value == "NeurIPS"


def test_build_ris_round_trips() -> None:
    from paper_ingestion.citation_format import build_ris

    text = build_ris(_paper(zotero_citation_key="Vaswani2017"))
    records = rispy.loads(text)

    assert len(records) == 1
    record = records[0]
    assert record["type_of_reference"] == "JOUR"
    assert record["title"] == "Attention Is All You Need"
    assert record["doi"] == "10.1/x"
    assert record["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert record["year"] == "2017"


def test_build_bibtex_omits_missing_metadata() -> None:
    """No doi/journal/year/authors → those fields are absent, no crash."""
    from paper_ingestion.citation_format import build_bibtex

    paper = _paper(paper_id=9, authors=[], published=None, metadata={}, url="")
    text = build_bibtex(paper)
    entry = bibtexparser.parse_string(text).entries[0]

    assert entry.key == "paper-9"  # falls back when no zotero key
    fields = entry.fields_dict
    assert "doi" not in fields
    assert "journal" not in fields
    assert "year" not in fields
    assert "author" not in fields
    assert fields["title"].value == "Attention Is All You Need"


def test_build_ris_omits_missing_metadata() -> None:
    from paper_ingestion.citation_format import build_ris

    paper = _paper(authors=[], published=None, metadata={}, url="")
    record = rispy.loads(build_ris(paper))[0]

    assert "doi" not in record
    assert "journal_name" not in record
    assert "year" not in record
    assert "authors" not in record


def test_build_citations_separates_entries_by_blank_line() -> None:
    from paper_ingestion.citation_format import CitationFormat, build_citations

    text = build_citations(
        [_paper(paper_id=1, zotero_citation_key="A"), _paper(paper_id=2, zotero_citation_key="B")],
        CitationFormat.BIBTEX,
    )
    assert "\n\n" in text
    assert len(bibtexparser.parse_string(text).entries) == 2


def test_build_bibtex_handles_unbalanced_braces() -> None:
    """Stray braces in source metadata must not truncate fields or break parsing."""
    from paper_ingestion.citation_format import build_bibtex

    paper = _paper(
        title="A messy } title { with braces",
        metadata={"doi": "10.1/a}b"},
        zotero_citation_key="Messy2020",
    )
    library = bibtexparser.parse_string(build_bibtex(paper))

    assert library.failed_blocks == []
    assert len(library.entries) == 1
    fields = library.entries[0].fields_dict
    assert "{" not in fields["title"].value and "}" not in fields["title"].value
    assert fields["title"].value == "A messy  title  with braces"
    assert fields["doi"].value == "10.1/ab"


def test_build_bibtex_resists_field_injection() -> None:
    """A crafted title cannot inject a sibling field via brace break-out."""
    from paper_ingestion.citation_format import build_bibtex

    paper = _paper(
        title="Real}, author={Mallory}, year={1999",
        authors=["Real Author"],
        metadata={},
        zotero_citation_key="Inj2020",
    )
    library = bibtexparser.parse_string(build_bibtex(paper))

    assert library.failed_blocks == []
    assert len(library.entries) == 1
    assert library.entries[0].fields_dict["author"].value == "Real Author"


def test_build_ris_collapses_newlines() -> None:
    """Embedded newlines must not split a RIS record across fields."""
    from paper_ingestion.citation_format import build_ris

    paper = _paper(title="Line one\nLine two", metadata={"doi": "10.1/x"})
    records = rispy.loads(build_ris(paper))

    assert len(records) == 1
    assert records[0]["title"] == "Line one Line two"


def test_build_bibtex_preserves_unicode() -> None:
    """Accented/non-Latin titles and author names survive a round-trip."""
    from paper_ingestion.citation_format import build_bibtex

    paper = _paper(
        title="Étude des réseaux — 北京 Über",
        authors=["Jean César", "Müller K", "Zhang 北"],
        zotero_citation_key="Cesar2013",
    )
    entry = bibtexparser.parse_string(build_bibtex(paper)).entries[0]

    assert entry.fields_dict["title"].value == "Étude des réseaux — 北京 Über"
    assert entry.fields_dict["author"].value == "Jean César and Müller K and Zhang 北"


def test_build_bibtex_handles_latex_special_chars() -> None:
    """& % $ # _ ~ ^ \\ and braces must not break the entry."""
    from paper_ingestion.citation_format import build_bibtex

    paper = _paper(title=r"Cost: 50% & $5 #1 a_b ~t ^c \b {x} }y{")
    library = bibtexparser.parse_string(build_bibtex(paper))

    assert library.failed_blocks == []
    assert len(library.entries) == 1
    title = library.entries[0].fields_dict["title"].value
    assert "{" not in title and "}" not in title


def test_build_bibtex_sanitizes_citation_key() -> None:
    """A zotero key with spaces/braces/commas/unicode yields a safe BibTeX key."""
    from paper_ingestion.citation_format import build_bibtex

    library = bibtexparser.parse_string(
        build_bibtex(_paper(zotero_citation_key="bad key{,}é@2020"))
    )
    key = library.entries[0].key
    assert key and all(c.isalnum() or c in "_:.-" for c in key)


def test_build_citations_bulk_round_trips_with_dirty_data() -> None:
    """A mixed batch (incl. stray braces) renders N independently-parseable entries."""
    from paper_ingestion.citation_format import CitationFormat, build_citations

    papers = [
        _paper(paper_id=1, zotero_citation_key="A", title="First}"),
        _paper(paper_id=2, zotero_citation_key="B", title="Second {x}"),
        _paper(paper_id=3, zotero_citation_key="C", authors=["Q W", "E R"]),
    ]
    library = bibtexparser.parse_string(build_citations(papers, CitationFormat.BIBTEX))

    assert library.failed_blocks == []
    assert sorted(e.key for e in library.entries) == ["A", "B", "C"]


def test_build_bibtex_handles_trailing_backslash() -> None:
    """A value ending in a backslash must not escape the field-closing brace.

    ``{value\\}`` makes the field run on, so bibtexparser drops the whole entry.
    Exercise every field that flows through ``_bibtex_value``.
    """
    from paper_ingestion.citation_format import build_bibtex

    papers = [
        _paper(zotero_citation_key="Back2020", title="Spin dynamics \\"),
        _paper(zotero_citation_key="Back2020", authors=["Ada Lovelace \\"]),
        _paper(zotero_citation_key="Back2020", url="https://example.test/x\\"),
        _paper(
            zotero_citation_key="Back2020", metadata={"doi": "10.1/x\\", "journal": "J Phys \\"}
        ),
    ]
    for paper in papers:
        library = bibtexparser.parse_string(build_bibtex(paper))
        assert library.failed_blocks == [], f"failed_blocks for {paper['title']!r}"
        assert len(library.entries) == 1, f"entry dropped for {paper['title']!r}"


def test_build_citations_bulk_survives_trailing_backslash() -> None:
    """One dirty paper (trailing backslash) must not corrupt sibling entries."""
    from paper_ingestion.citation_format import CitationFormat, build_citations

    papers = [
        _paper(paper_id=1, zotero_citation_key="Good1"),
        _paper(paper_id=2, zotero_citation_key="Bad2", title="Ends in a backslash \\"),
        _paper(paper_id=3, zotero_citation_key="Good3"),
    ]
    library = bibtexparser.parse_string(build_citations(papers, CitationFormat.BIBTEX))

    assert library.failed_blocks == []
    assert sorted(e.key for e in library.entries) == ["Bad2", "Good1", "Good3"]


def test_build_bibtex_coerces_non_string_metadata() -> None:
    """Non-string JSONB metadata (e.g. a numeric doi) must not raise."""
    from paper_ingestion.citation_format import build_bibtex

    paper = _paper(metadata={"doi": 123456, "journal": 2024}, zotero_citation_key="Num2021")
    library = bibtexparser.parse_string(build_bibtex(paper))

    assert library.failed_blocks == []
    fields = library.entries[0].fields_dict
    assert fields["doi"].value == "123456"
    assert fields["journal"].value == "2024"


def test_build_ris_coerces_non_string_metadata() -> None:
    """Non-string JSONB metadata must not raise in the RIS path either."""
    from paper_ingestion.citation_format import build_ris

    record = rispy.loads(
        build_ris(_paper(metadata={"doi": 123456}, zotero_citation_key="Num2021"))
    )[0]

    assert record["doi"] == "123456"


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


def _override_app(app, pool, user_id: int):
    from jarvis_common.auth import (
        current_user_id_strict_with_owner_override,
        verify_api_key,
    )
    from paper_ingestion.deps import get_db_pool

    app.state.db_pool = pool
    app.state.limiter.enabled = False

    async def _db_pool():
        return pool

    async def _api_key():
        return None

    app.dependency_overrides[get_db_pool] = _db_pool
    app.dependency_overrides[verify_api_key] = _api_key
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: user_id


@pytest.fixture
def asgi_client():
    import httpx
    from httpx import ASGITransport

    from paper_ingestion.main import app

    def _make(pool, user_id: int = 1):
        _override_app(app, pool, user_id)
        return (
            httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
            app,
        )

    yield _make
    app.dependency_overrides.clear()
    from paper_ingestion.main import app as a

    a.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_get_citation_returns_bibtex_with_headers(asgi_client) -> None:
    pool, conn = make_pool_and_conn()
    conn.fetchrow = AsyncMock(
        side_effect=[
            FakeRecord({"id": 5, "is_visible": True}),
            _paper(paper_id=5, zotero_citation_key="Key2017"),  # papers SELECT *
        ]
    )
    client, _app = asgi_client(pool, user_id=1)
    async with client:
        resp = await client.get("/api/papers/5/citation?format=bibtex")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-bibtex")
    assert resp.headers["content-disposition"] == 'attachment; filename="Key2017.bib"'
    assert "@article{Key2017" in resp.text


@pytest.mark.asyncio
async def test_get_citation_ris_content_type(asgi_client) -> None:
    pool, conn = make_pool_and_conn()
    conn.fetchrow = AsyncMock(
        side_effect=[FakeRecord({"id": 5, "is_visible": True}), _paper(paper_id=5)]
    )
    client, _app = asgi_client(pool, user_id=1)
    async with client:
        resp = await client.get("/api/papers/5/citation?format=ris")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-research-info-systems")
    assert resp.headers["content-disposition"].endswith('.ris"')
    assert "TY  - JOUR" in resp.text


@pytest.mark.asyncio
async def test_get_citation_invalid_format_422(asgi_client) -> None:
    pool, _conn = make_pool_and_conn()
    client, _app = asgi_client(pool, user_id=1)
    async with client:
        resp = await client.get("/api/papers/5/citation?format=mla")
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_get_citation_non_owner_404(asgi_client) -> None:
    """A paper the caller cannot see → assert_paper_ownership 404."""
    pool, conn = make_pool_and_conn()
    # assert_paper_ownership: papers row missing → 404
    conn.fetchrow = AsyncMock(return_value=None)
    client, _app = asgi_client(pool, user_id=2)
    async with client:
        resp = await client.get("/api/papers/999/citation")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_bulk_citations_returns_only_visible_in_order(asgi_client) -> None:
    pool, conn = make_pool_and_conn()
    # _filter_visible_paper_ids returns [3, 1] (2 invisible); rows come back
    # unordered → handler must re-order to input order [1, 3].
    conn.fetch = AsyncMock(
        side_effect=[
            [FakeRecord({"id": 3}), FakeRecord({"id": 1})],  # visible filter
            [
                _paper(paper_id=3, title="Third", zotero_citation_key="C3"),
                _paper(paper_id=1, title="First", zotero_citation_key="C1"),
            ],  # papers SELECT
        ]
    )
    client, _app = asgi_client(pool, user_id=1)
    async with client:
        resp = await client.post(
            "/api/papers/citations",
            json={"paper_ids": [1, 2, 3], "format": "bibtex"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"] == 'attachment; filename="citations.bib"'
    keys = [e.key for e in bibtexparser.parse_string(resp.text).entries]
    assert keys == ["C1", "C3"], "entries follow input order, invisible id 2 dropped"


@pytest.mark.asyncio
async def test_bulk_citations_none_visible_404(asgi_client) -> None:
    pool, conn = make_pool_and_conn()
    conn.fetch = AsyncMock(return_value=[])  # _filter_visible_paper_ids → empty
    client, _app = asgi_client(pool, user_id=1)
    async with client:
        resp = await client.post(
            "/api/papers/citations",
            json={"paper_ids": [99], "format": "ris"},
        )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Markdown knowledge export
# ---------------------------------------------------------------------------


class _ScopedConn:
    """Fake connection that enforces the per-user scoping contract.

    Rows carrying a ``user_id`` are only returned when the query both names
    ``user_id`` and binds the caller's id as ``$2``, so an unscoped read fails
    here instead of silently exporting another tenant's workspace.
    """

    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self._tables = tables

    _SCOPED = re.compile(r"user_id (?:=|IS NOT DISTINCT FROM) \$2")

    def _select(self, sql: str, args: tuple) -> list[FakeRecord]:
        table = next(name for name in self._tables if name in sql)
        rows = [r for r in self._tables[table] if r["paper_id"] == args[0]]
        if any("user_id" in r for r in rows):
            assert self._SCOPED.search(sql), f"unscoped query for {table}: {sql}"
            rows = [r for r in rows if r.get("user_id") == args[1]]
        return [FakeRecord(r) for r in rows]

    async def fetch(self, sql: str, *args) -> list[FakeRecord]:
        return self._select(sql, args)

    async def fetchrow(self, sql: str, *args) -> FakeRecord | None:
        rows = self._select(sql, args)
        return rows[0] if rows else None


def _markdown_tables(**overrides) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {
        "papers": [dict(_paper(paper_id=5, zotero_citation_key="Key2017"), paper_id=5)],
        "paper_summaries": [
            {
                "paper_id": 5,
                "user_id": 1,
                "summary_detailed": "A detailed summary.",
                "methodology": "Ablation study.",
                "limitations": "English only.",
            }
        ],
        "paper_notes": [
            {
                "paper_id": 5,
                "user_id": 1,
                "user_note": "My own note.",
                "highlight_text": "a quoted span",
                "page_number": 3,
            },
            {
                "paper_id": 5,
                "user_id": 2,
                "user_note": "OTHER USER SECRET NOTE",
                "highlight_text": "OTHER USER SECRET HIGHLIGHT",
                "page_number": 9,
            },
        ],
        "cards": [
            {"paper_id": 5, "user_id": 1, "front": "What is attention?", "back": "A weighting."},
            {"paper_id": 5, "user_id": 2, "front": "OTHER USER SECRET CARD", "back": "nope"},
        ],
        "paper_extractions": [
            {"paper_id": 5, "user_id": 1, "extractions": {"dataset": {"value": "WMT14"}}},
            {"paper_id": 5, "user_id": 2, "extractions": {"dataset": "OTHER USER SECRET DATASET"}},
        ],
    }
    return tables | overrides


async def _build(tables: dict[str, list[dict]], user_id: int = 1):
    from paper_ingestion.services.markdown_export import build_paper_markdown

    return await build_paper_markdown(_ScopedConn(tables), 5, user_id)


@pytest.mark.asyncio
async def test_markdown_export_renders_all_sections() -> None:
    export = await _build(_markdown_tables())

    assert export.stem == "attention-is-all-you-need"
    assert "# Attention Is All You Need" in export.text
    assert 'title: "Attention Is All You Need"' in export.text
    assert "A detailed summary." in export.text
    assert "### Methodology\n\nAblation study." in export.text
    assert "### Limitations\n\nEnglish only." in export.text
    assert "- My own note. (p. 3)\n  > a quoted span" in export.text
    assert "### What is attention?\n\nA weighting." in export.text
    assert "| dataset | WMT14 |" in export.text
    assert "```bibtex\n@article{Key2017" in export.text
    assert "<" not in export.text, "Obsidian export must be plain Markdown, no HTML"


@pytest.mark.asyncio
async def test_markdown_export_excludes_another_users_workspace() -> None:
    """The decisive tenancy assertion: user 2's rows never reach user 1's export."""
    export = await _build(_markdown_tables(), user_id=1)

    assert "My own note." in export.text
    for secret in (
        "OTHER USER SECRET NOTE",
        "OTHER USER SECRET HIGHLIGHT",
        "OTHER USER SECRET CARD",
        "OTHER USER SECRET DATASET",
    ):
        assert secret not in export.text


@pytest.mark.asyncio
async def test_markdown_export_placeholder_when_not_summarized() -> None:
    from paper_ingestion.services.markdown_export import NO_SUMMARY_PLACEHOLDER

    export = await _build(_markdown_tables(paper_summaries=[]))

    assert NO_SUMMARY_PLACEHOLDER in export.text
    assert "not yet been summarized" in export.text
    assert "```bibtex" in export.text, "an unsummarized paper still exports its citation"


@pytest.mark.asyncio
async def test_markdown_export_endpoint_headers(asgi_client) -> None:
    pool, conn = make_pool_and_conn()
    conn.fetchrow = AsyncMock(
        side_effect=[
            FakeRecord({"id": 5, "is_visible": True}),
            _paper(paper_id=5, zotero_citation_key="Key2017"),  # papers JOIN
            None,  # paper_summaries → placeholder path
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    client, _app = asgi_client(pool, user_id=1)
    async with client:
        resp = await client.get("/api/papers/5/export.md")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["content-disposition"] == (
        'attachment; filename="attention-is-all-you-need.md"'
    )
    assert resp.text.startswith("---\n")


@pytest.mark.asyncio
async def test_markdown_export_non_owner_404(asgi_client) -> None:
    pool, conn = make_pool_and_conn()
    conn.fetchrow = AsyncMock(return_value=None)  # assert_paper_ownership → 404
    client, _app = asgi_client(pool, user_id=2)
    async with client:
        resp = await client.get("/api/papers/999/export.md")
    assert resp.status_code == 404, resp.text
