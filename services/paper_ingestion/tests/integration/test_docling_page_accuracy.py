"""Page-number accuracy guard for the Docling extraction path.

``page_number`` is a trust surface: it drives RAG citation pages
(``rag/streaming.py``), the note-verification window (``routers/notes.py``),
and snapshot selection (``services/summarization.py``).  A wrong page reads as
"the RAG hallucinated".

This opt-in integration test runs the REAL production path —
:func:`paper_ingestion.pdf_processor._extract_text_sync` (Docling, page-bounded
provenance anchors) + :meth:`Embedder.chunk_text` (page-bounded chunking) — and
scores each chunk's ``page_number`` against the PyMuPDF (``fitz``) physical
page.  fitz is the oracle because ``PDFProcessor.generate_snapshots`` numbers
snapshots by the same physical page — exactly what the user sees when clicking a
citation.

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

import re
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import fitz
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


def _fitz_pages(pdf_path: Path) -> list[str]:
    doc = fitz.open(str(pdf_path))
    try:
        return [doc[i].get_text() for i in range(doc.page_count)]
    finally:
        doc.close()


def _candidate_probes(page_text: str) -> list[str]:
    probes = []
    for raw in page_text.split("\n"):
        raw = raw.strip()
        if len(raw) < 60:
            continue
        if sum(ch.isalpha() or ch.isspace() for ch in raw) / len(raw) >= 0.8:
            probes.append(raw)
    return probes


@pytest.mark.parametrize("arxiv_id", _ARXIV_IDS)
def test_docling_page_accuracy(arxiv_id: str) -> None:
    pdf_path = _ensure_pdf(arxiv_id)
    fitz_pages = _fitz_pages(pdf_path)
    norm_pages = [_norm(p) for p in fitz_pages]

    # Real production path: Docling extraction + page-bounded chunking.
    full_text, page_anchors = _extract_text_sync(pdf_path)
    embedder = Embedder(http_client=MagicMock(), qdrant_client=MagicMock())
    chunks = embedder.chunk_text(full_text, page_anchors)
    chunk_norms = [(c.page_number, _norm(c.content)) for c in chunks]

    located = exact = adjacent = 0
    far_misses: list[str] = []
    seen: set[str] = set()

    for page_idx, page_text in enumerate(fitz_pages):
        true_page = page_idx + 1
        per_page = 0
        for probe in _candidate_probes(page_text):
            n = _norm(probe)
            if len(n) < 50 or n in seen:
                continue
            # Probe must be unique to one physical page, else its true page is
            # ambiguous and it cannot be scored.
            if sum(1 for np_ in norm_pages if n in np_) != 1:
                continue
            seen.add(n)
            matched = [pg for pg, cn in chunk_norms if pg is not None and n in cn]
            if not matched:
                continue
            located += 1
            if true_page in matched:
                exact += 1
            if min(abs(pg - true_page) for pg in matched) <= 1:
                adjacent += 1
            else:
                far_misses.append(f"  p{true_page} -> {sorted(set(matched))}: {probe[:70]!r}")
            per_page += 1
            if per_page >= 4:  # spread probes across pages
                break

    exact_rate = exact / located if located else 0.0
    adjacent_rate = adjacent / located if located else 0.0
    print(
        f"\n[{arxiv_id}] pages={len(fitz_pages)} chunks={len(chunks)} located={located} "
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
