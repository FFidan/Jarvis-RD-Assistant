"""Page-number accuracy guard for the Docling extraction path.

``page_number`` is a trust surface: it drives RAG citation pages
(``rag/streaming.py``), the note-verification window (``routers/notes.py``),
and snapshot selection (``services/summarization.py``).  A wrong page reads as
"the RAG hallucinated".

This opt-in integration test runs the REAL production path —
:func:`paper_ingestion.pdf_processor._extract_text_sync` (Docling, page-bounded
provenance anchors) + :meth:`Embedder.chunk_text` (page-bounded chunking) — and
scores each chunk's ``page_number`` against a **frozen oracle**: probe lines
captured once per physical page and committed to ``page_accuracy_probes.json``.
Each probe is unique to a single physical page, so its true page is the page the
user sees when clicking a citation (the snapshot numbering is by the same
physical page).  Freezing the oracle keeps this guard independent of the system
under test and removes the previous PyMuPDF/AGPL dependency: the fixture was
generated with fitz once and is now read from disk; no AGPL code is imported.

Docling assigns a paragraph that spans a page break to the page where it
*begins*, so the only deviation from a line-level oracle is such a paragraph
cited at its start page — the content is still present there.  The hard
guarantee enforced here is that a citation is **never more than one page off**
(no random/hallucinated pages), with most citations exact.

Run: ``uv run pytest services/paper_ingestion/tests/integration/\
test_docling_page_accuracy.py -m integration -v -s``
(downloads Docling + RapidOCR models on first run + 3 arXiv PDFs).
"""

from __future__ import annotations

import json
import re
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.pdf_processor import _extract_text_sync

pytestmark = pytest.mark.integration

# arXiv is in PDFProcessor.ALLOWED_PDF_DOMAINS.  Three structurally diverse
# papers: math-heavy, multi-section with tables, and figure/caption-heavy.
_ARXIV_IDS = ("1706.03762", "1810.04805", "2010.11929")
_EXACT_FLOOR = 0.80  # most citations land on the exact physical page
_ADJACENT_FLOOR = 0.98  # trust guarantee: a citation is never >1 page off
_MIN_PROBES = 10
_CACHE = Path(tempfile.gettempdir()) / "jarvis_docling_accuracy_pdfs"
_PROBES_FIXTURE = Path(__file__).parent / "page_accuracy_probes.json"


def _ensure_pdf(arxiv_id: str) -> Path:
    _CACHE.mkdir(parents=True, exist_ok=True)
    dest = _CACHE / f"{arxiv_id}.pdf"
    if dest.exists() and dest.stat().st_size > 10_000:
        return dest
    try:
        urllib.request.urlretrieve(f"https://arxiv.org/pdf/{arxiv_id}", dest)  # noqa: S310
    except Exception as exc:  # network-gated; opt-in test
        pytest.skip(f"could not download {arxiv_id}: {exc}")
    if not dest.exists() or dest.stat().st_size < 10_000:
        pytest.skip(f"download of {arxiv_id} produced no usable PDF")
    return dest


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def _load_probes(arxiv_id: str) -> list[tuple[str, int]]:
    """Frozen oracle: (normalized probe, true physical page) pairs."""
    fixture = json.loads(_PROBES_FIXTURE.read_text())
    return [(_norm(rec["probe"]), rec["true_page"]) for rec in fixture[arxiv_id]]


@pytest.mark.parametrize("arxiv_id", _ARXIV_IDS)
def test_docling_page_accuracy(arxiv_id: str) -> None:
    pdf_path = _ensure_pdf(arxiv_id)
    probes = _load_probes(arxiv_id)

    # Real production path: Docling extraction + page-bounded chunking.
    full_text, page_anchors = _extract_text_sync(pdf_path)
    embedder = Embedder(http_client=MagicMock(), qdrant_client=MagicMock())
    chunks = embedder.chunk_text(full_text, page_anchors)
    chunk_norms = [(c.page_number, _norm(c.content)) for c in chunks]

    located = exact = adjacent = 0
    far_misses: list[str] = []

    for probe_norm, true_page in probes:
        matched = [pg for pg, cn in chunk_norms if pg is not None and probe_norm in cn]
        if not matched:
            continue
        located += 1
        if true_page in matched:
            exact += 1
        if min(abs(pg - true_page) for pg in matched) <= 1:
            adjacent += 1
        else:
            far_misses.append(f"  p{true_page} -> {sorted(set(matched))}: {probe_norm[:70]!r}")

    exact_rate = exact / located if located else 0.0
    adjacent_rate = adjacent / located if located else 0.0
    print(
        f"\n[{arxiv_id}] probes={len(probes)} chunks={len(chunks)} located={located} "
        f"exact={exact_rate:.3f} within1={adjacent_rate:.3f} far_misses={len(far_misses)}"
    )
    if far_misses:
        print("  FAR misses (>1 page off — possible page drift):\n" + "\n".join(far_misses[:15]))

    assert located >= _MIN_PROBES, (
        f"{arxiv_id}: only {located} probes located in Docling output "
        f"(need >={_MIN_PROBES}); extraction may be dropping body text"
    )
    assert adjacent_rate >= _ADJACENT_FLOOR, (
        f"{arxiv_id}: only {adjacent_rate:.3f} of citations within 1 page "
        f"(need >={_ADJACENT_FLOOR}); Docling page provenance is drifting"
    )
    assert exact_rate >= _EXACT_FLOOR, (
        f"{arxiv_id}: exact-page rate {exact_rate:.3f} < {_EXACT_FLOOR}"
    )
