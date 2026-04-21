"""Module-level holders for service-scoped singletons.

These are set once during FastAPI lifespan startup and read by job handlers
that run in-process but must not import ``paper_ingestion.main`` (which would
create a circular dependency).

Usage
-----
In ``main.py`` lifespan::

    from paper_ingestion._state import svc

    svc.pdf_processor = PDFProcessor(...)
    svc.embedder = Embedder(...)
    svc.verifier = QuoteVerifier()
    svc.sources = {}

In job handlers::

    from paper_ingestion._state import svc

    embedder = svc.embedder
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.pdf_processor import PDFProcessor
    from paper_ingestion.verification import QuoteVerifier


class _ServiceState:
    """Holds references to long-lived service objects populated during lifespan."""

    pdf_processor: PDFProcessor | None = None
    embedder: Embedder | None = None
    verifier: QuoteVerifier | None = None
    sources: dict[str, Any] | None = None


svc = _ServiceState()
