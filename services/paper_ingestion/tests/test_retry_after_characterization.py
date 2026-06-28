"""Characterization tests for the (pre-consolidation) Retry-After parsers.

This module pins the CURRENT behaviour of the four divergent ``Retry-After``
parsers BEFORE they are consolidated onto a single ``jarvis_common.net``
helper, then is updated to reflect the now-unified behaviour.

The four parsers under characterization:

* ``base.parse_retry_after(exc)``       — exception in; delta-seconds only; int.
* ``PaperSource._retry_after_seconds``  — response in; delta-seconds only;
  capped at ``_MAX_RETRY_AFTER_S`` (3600); int.
* ``arxiv._retry_after_s(value)``       — string in; delta + HTTP-date; float;
  uncapped (the arXiv call site applies its own 60 s cap).
* ``zotero._parse_retry_after(value)``  — string in; delta + HTTP-date; float;
  capped at ``_MAX_RETRY_AFTER_SECONDS`` (60).

Pre-consolidation, the two ``base`` parsers handle ONLY delta-seconds and
return ``None`` on an HTTP-date input; the arXiv and Zotero parsers handle
BOTH RFC-7231 forms. After consolidation all four understand BOTH forms.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from paper_ingestion.integrations.zotero_client import (
    _MAX_RETRY_AFTER_SECONDS as ZOTERO_CAP,
)
from paper_ingestion.integrations.zotero_client import (
    _parse_retry_after as zotero_parse,
)
from paper_ingestion.sources.arxiv_source import _retry_after_s as arxiv_parse
from paper_ingestion.sources.base import (
    _MAX_RETRY_AFTER_S as BASE_CAP,
)
from paper_ingestion.sources.base import (
    PaperSource,
    parse_retry_after as base_parse_exc,
)


class _StubSource(PaperSource):
    """Minimal concrete PaperSource for exercising the base response parser."""

    source_type = "stub"

    async def search(self, query: str, max_results: int = 10, **kwargs):  # type: ignore[override]
        return []

    async def fetch_by_id(self, external_id: str):
        return None


@pytest.fixture()
def stub_source() -> _StubSource:
    return _StubSource(config=MagicMock(), http_client=MagicMock())


# An RFC 1123 HTTP-date far in the future (delta will exceed every cap).
_HTTP_DATE_FUTURE = "Wed, 21 Oct 9999 07:28:00 GMT"
# A fixed delta-seconds value within all caps.
_DELTA = "120"
# A value exceeding both caps (60 and 3600).
_OVER_CAP = "999999"


def _exc_with_retry_after(value: str | None) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError whose response carries (or omits) Retry-After."""
    request = httpx.Request("GET", "https://example.test")
    headers = {"Retry-After": value} if value is not None else {}
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def _response_with_retry_after(value: str | None) -> httpx.Response:
    headers = {"Retry-After": value} if value is not None else {}
    return httpx.Response(429, headers=headers)


# ---------------------------------------------------------------------------
# base.parse_retry_after(exc) — delta-seconds only, no cap, int
# ---------------------------------------------------------------------------


def test_base_exc_parser_delta_seconds() -> None:
    assert base_parse_exc(_exc_with_retry_after(_DELTA)) == 120


def test_base_exc_parser_none_and_garbage() -> None:
    assert base_parse_exc(_exc_with_retry_after(None)) is None
    assert base_parse_exc(_exc_with_retry_after("garbage")) is None
    assert base_parse_exc(RuntimeError("boom")) is None  # no .response attr


def test_base_exc_parser_http_date_now_supported() -> None:
    # Post-consolidation, the exception parser ALSO understands HTTP-date.
    result = base_parse_exc(_exc_with_retry_after(_HTTP_DATE_FUTURE))
    assert result is not None
    assert result <= BASE_CAP


def test_base_exc_parser_caps_over_cap_value() -> None:
    # The exception parser now caps at _MAX_RETRY_AFTER_S (was uncapped).
    assert base_parse_exc(_exc_with_retry_after(_OVER_CAP)) == BASE_CAP


# ---------------------------------------------------------------------------
# PaperSource._retry_after_seconds(response) — cap 3600, int
# ---------------------------------------------------------------------------


def test_base_response_parser_delta_and_cap(stub_source) -> None:
    assert stub_source._retry_after_seconds(_response_with_retry_after(_DELTA)) == 120
    assert stub_source._retry_after_seconds(_response_with_retry_after(_OVER_CAP)) == BASE_CAP


def test_base_response_parser_none_and_garbage(stub_source) -> None:
    assert stub_source._retry_after_seconds(None) is None
    assert stub_source._retry_after_seconds(_response_with_retry_after(None)) is None
    assert stub_source._retry_after_seconds(_response_with_retry_after("garbage")) is None


def test_base_response_parser_http_date_now_supported(stub_source) -> None:
    # The response parser ALSO understands HTTP-date and stays capped.
    result = stub_source._retry_after_seconds(_response_with_retry_after(_HTTP_DATE_FUTURE))
    assert result is not None
    assert result <= BASE_CAP


# ---------------------------------------------------------------------------
# arxiv._retry_after_s(value) — delta + HTTP-date, uncapped, float
# ---------------------------------------------------------------------------


def test_arxiv_parser_both_forms() -> None:
    assert arxiv_parse(_DELTA) == 120.0
    http_date = arxiv_parse(_HTTP_DATE_FUTURE)
    assert http_date is not None and http_date > 0


def test_arxiv_parser_none_and_garbage() -> None:
    assert arxiv_parse(None) is None
    assert arxiv_parse("") is None
    assert arxiv_parse("garbage") is None


def test_arxiv_parser_uncapped() -> None:
    # The arXiv module parser stays uncapped; the call site applies the 60 s cap.
    assert arxiv_parse(_OVER_CAP) == float(_OVER_CAP)


# ---------------------------------------------------------------------------
# zotero._parse_retry_after(value) — delta + HTTP-date, cap 60, float
# ---------------------------------------------------------------------------


def test_zotero_parser_both_forms_and_cap() -> None:
    # A delta below the 60 s Zotero cap passes through unchanged.
    assert zotero_parse("30") == 30.0
    # _DELTA (120) exceeds the Zotero cap and is clamped to it.
    assert zotero_parse(_DELTA) == ZOTERO_CAP
    assert zotero_parse(_OVER_CAP) == ZOTERO_CAP
    http_date = zotero_parse(_HTTP_DATE_FUTURE)
    assert http_date is not None and http_date <= ZOTERO_CAP


def test_zotero_parser_none_negative_garbage() -> None:
    assert zotero_parse(None) is None
    assert zotero_parse("") is None
    assert zotero_parse("garbage") is None
    # Zotero rejects negative deltas outright.
    assert zotero_parse("-1") is None


def test_zotero_parser_zero_kept() -> None:
    assert zotero_parse("0") == 0.0


@pytest.mark.parametrize(
    "cap,parser",
    [(60.0, ZOTERO_CAP), (3600, BASE_CAP)],
)
def test_caps_are_distinct(cap, parser) -> None:
    """Pin the two distinct caps the sources rely on (60 s Zotero / 3600 s base)."""
    assert cap == parser
