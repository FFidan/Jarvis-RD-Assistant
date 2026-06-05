"""PI-DISC-002 / PI-DISC-003 — author-parameter injection hardening.

PI-DISC-002 (PubMed): the ``author`` param is interpolated into an NCBI
E-utilities ``term`` string as ``{query} AND {author}[Author]``.  An author
value containing ``"`` allows escaping out of any quoting or altering the query
syntax (e.g. ``Smith"] AND malicious[Title``).  The fix wraps the author in
double-quotes after stripping embedded ``"`` so NCBI treats the value as a
phrase literal.

PI-DISC-003 (OpenAlex): the ``author`` param is appended to a comma-separated
OpenAlex filter string.  A comma in author injects an extra filter clause; a
pipe or plus injects OR/AND logic.  The fix strips ``,``, ``|``, and ``+``
from author before interpolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.sources.openalex_source import OpenAlexSource
from paper_ingestion.sources.pubmed_source import PubMedSource


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config(source_type: SourceType) -> PaperSourceConfig:
    return PaperSourceConfig(id=1, source_type=source_type, enabled=True, config={})


def _make_pubmed() -> PubMedSource:
    return PubMedSource(config=_make_config(SourceType.PUBMED), http_client=MagicMock())


def _make_openalex(*, api_key: str = "test-key") -> OpenAlexSource:
    src = OpenAlexSource(config=_make_config(SourceType.OPENALEX), http_client=MagicMock())
    # Inject key directly to bypass settings lookup in tests
    src._api_key = api_key
    src._email = ""
    return src


# ---------------------------------------------------------------------------
# PI-DISC-002 — PubMed author-param injection
# ---------------------------------------------------------------------------


class TestPubMedAuthorInjection:
    """The author value must not be able to inject extra NCBI query clauses."""

    @pytest.mark.asyncio
    async def test_plain_author_includes_author_field_tag(self) -> None:
        """A plain author name is passed through and wrapped in the [Author] tag."""
        src = _make_pubmed()
        captured: list[str] = []

        async def fake_esearch(term: str, retmax: int, extra=None) -> list[str]:
            captured.append(term)
            return []

        src._esearch = fake_esearch  # type: ignore[method-assign]
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        await src.search("machine learning", author="Smith J")

        assert captured, "esearch was not called"
        term = captured[0]
        assert '"Smith J"[Author]' in term
        assert 'machine learning AND "Smith J"[Author]' == term

    @pytest.mark.asyncio
    async def test_author_with_double_quotes_cannot_break_out_of_quoted_phrase(self) -> None:
        """Double-quotes inside the author value are stripped, not escaped-into-syntax."""
        src = _make_pubmed()
        captured: list[str] = []

        async def fake_esearch(term: str, retmax: int, extra=None) -> list[str]:
            captured.append(term)
            return []

        src._esearch = fake_esearch  # type: ignore[method-assign]
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        # Attempt injection: close the phrase, inject a Title clause
        malicious_author = 'Smith"] AND malicious[Title'
        await src.search("cancer", author=malicious_author)

        term = captured[0]
        # The injected closing quote + extra clause must NOT appear literally in term
        assert '"] AND malicious[Title' not in term
        # The term must still be syntactically wrapped in double-quotes
        assert term.startswith('cancer AND "')
        assert term.endswith('"[Author]')
        # The stripped author body must not contain any double-quotes
        inner = term[len('cancer AND "') : -len('"[Author]')]
        assert '"' not in inner

    @pytest.mark.asyncio
    async def test_author_with_only_quotes_produces_empty_quoted_phrase(self) -> None:
        """An author that is purely double-quotes becomes an empty phrase literal."""
        src = _make_pubmed()
        captured: list[str] = []

        async def fake_esearch(term: str, retmax: int, extra=None) -> list[str]:
            captured.append(term)
            return []

        src._esearch = fake_esearch  # type: ignore[method-assign]
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        await src.search("query", author='"""')

        term = captured[0]
        # All quotes stripped → empty phrase literal
        assert '""[Author]' in term

    @pytest.mark.asyncio
    async def test_no_author_does_not_alter_query(self) -> None:
        """When author is None the term equals the original query unchanged."""
        src = _make_pubmed()
        captured: list[str] = []

        async def fake_esearch(term: str, retmax: int, extra=None) -> list[str]:
            captured.append(term)
            return []

        src._esearch = fake_esearch  # type: ignore[method-assign]
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        await src.search("neural networks", author=None)

        assert captured[0] == "neural networks"


# ---------------------------------------------------------------------------
# PI-DISC-003 — OpenAlex author-param injection
# ---------------------------------------------------------------------------


class TestOpenAlexAuthorInjection:
    """The author value must not inject extra filter clauses via comma/pipe/plus."""

    def _capture_filter(self, src: OpenAlexSource, author: str) -> str | None:
        """
        Synchronously exercise the filter-building path without HTTP or rate-limiting.

        Rebuilds only the filter construction block from search() so we can
        assert on the filter string directly without needing to mock HTTP.
        """
        filters: list[str] = []
        safe_author = author.replace(",", "").replace("|", "").replace("+", "")
        filters.append(f"author.display_name.search:{safe_author}")
        return ",".join(filters) if filters else None

    @pytest.mark.asyncio
    async def test_plain_author_builds_correct_filter(self) -> None:
        """A plain author name produces a well-formed filter clause."""
        src = _make_openalex()
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        # Capture the params passed to http_client.get
        captured_params: list[dict] = []

        async def fake_get(url: str, *, params: dict, timeout: float):
            captured_params.append(params)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"results": [], "meta": {"count": 0}}
            resp.raise_for_status = MagicMock()
            return resp

        src.http_client.get = fake_get  # type: ignore[method-assign]

        await src.search("deep learning", author="LeCun Y")

        assert captured_params, "http_client.get was not called"
        filt = captured_params[0].get("filter", "")
        assert "author.display_name.search:LeCun Y" in filt

    @pytest.mark.asyncio
    async def test_author_with_comma_cannot_inject_extra_filter_clause(self) -> None:
        """A comma in author is stripped, preventing injection of a second filter clause."""
        src = _make_openalex()
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        captured_params: list[dict] = []

        async def fake_get(url: str, *, params: dict, timeout: float):
            captured_params.append(params)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"results": [], "meta": {"count": 0}}
            resp.raise_for_status = MagicMock()
            return resp

        src.http_client.get = fake_get  # type: ignore[method-assign]

        # Attempt injection: the comma would start a new filter pair
        malicious_author = "Smith,open_access.is_oa:true"
        await src.search("cancer", author=malicious_author)

        assert captured_params, "http_client.get was not called"
        filt = captured_params[0].get("filter", "")
        # No comma-separated clause starting with "open_access" must appear —
        # that would mean the injected fragment became a separate filter pair.
        # Splitting on comma should not yield any clause that is NOT an
        # author.display_name or a publication_date filter.
        clauses = filt.split(",") if filt else []
        for clause in clauses:
            assert clause.startswith("author.display_name") or "publication_date" in clause, (
                f"Unexpected injected clause in filter: {clause!r}"
            )

    @pytest.mark.asyncio
    async def test_author_with_pipe_cannot_inject_or_logic(self) -> None:
        """A pipe in author is stripped, preventing OR-logic injection."""
        src = _make_openalex()
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        captured_params: list[dict] = []

        async def fake_get(url: str, *, params: dict, timeout: float):
            captured_params.append(params)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"results": [], "meta": {"count": 0}}
            resp.raise_for_status = MagicMock()
            return resp

        src.http_client.get = fake_get  # type: ignore[method-assign]

        malicious_author = "Smith|Jones"
        await src.search("biology", author=malicious_author)

        assert captured_params
        filt = captured_params[0].get("filter", "")
        # After stripping the pipe the author value should not contain |
        author_part = next((p for p in filt.split(",") if "author.display_name" in p), "")
        assert "|" not in author_part

    @pytest.mark.asyncio
    async def test_author_with_plus_cannot_inject_and_logic(self) -> None:
        """A plus in author is stripped, preventing AND-logic injection."""
        src = _make_openalex()
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        captured_params: list[dict] = []

        async def fake_get(url: str, *, params: dict, timeout: float):
            captured_params.append(params)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"results": [], "meta": {"count": 0}}
            resp.raise_for_status = MagicMock()
            return resp

        src.http_client.get = fake_get  # type: ignore[method-assign]

        malicious_author = "Smith+Jones"
        await src.search("medicine", author=malicious_author)

        assert captured_params
        filt = captured_params[0].get("filter", "")
        author_part = next((p for p in filt.split(",") if "author.display_name" in p), "")
        assert "+" not in author_part

    @pytest.mark.asyncio
    async def test_no_author_omits_author_filter(self) -> None:
        """When author is None the filter contains no author clause."""
        src = _make_openalex()
        src._rate_limiter = AsyncMock()
        src._rate_limiter.acquire = AsyncMock()

        captured_params: list[dict] = []

        async def fake_get(url: str, *, params: dict, timeout: float):
            captured_params.append(params)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"results": [], "meta": {"count": 0}}
            resp.raise_for_status = MagicMock()
            return resp

        src.http_client.get = fake_get  # type: ignore[method-assign]

        await src.search("genomics", author=None)

        assert captured_params
        filt = captured_params[0].get("filter", "")
        assert "author.display_name" not in filt
