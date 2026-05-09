"""Module-level holders for service-scoped singletons.

These are set once during FastAPI lifespan startup and read by job handlers
that run in-process but must not import ``paper_ingestion.main`` (which would
create a circular dependency).

Usage
-----
In ``main.py`` lifespan::

    from paper_ingestion._state import set_services

    set_services(
        pdf_processor=PDFProcessor(...),
        embedder=Embedder(...),
        verifier=QuoteVerifier(),
        sources={},
    )

In job handlers::

    from paper_ingestion._state import get_services

    embedder = get_services().embedder
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis_common.verify import QuoteVerifier

    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.pdf_processor import PDFProcessor


_UNSET = object()


@dataclass(slots=True)
class PaperIngestionServices:
    """Long-lived paper-ingestion collaborators populated during lifespan.

    Attributes
    ----------
    pdf_processor:
        PDF download and extraction service used by background jobs.
    embedder:
        Vector embedding/search collaborator shared by routes and jobs.
    verifier:
        Quote verifier used by extraction, summarization, and contradiction flows.
    sources:
        Source plugin instances keyed by source type.
    openai_client:
        Instructor-patched OpenAI client configured for structured LLM calls.
    """

    pdf_processor: PDFProcessor | None = None
    embedder: Embedder | None = None
    verifier: QuoteVerifier | None = None
    sources: dict[str, Any] | None = None
    openai_client: Any | None = None  # Instructor-patched openai.AsyncOpenAI, set in lifespan


svc = PaperIngestionServices()


def get_services() -> PaperIngestionServices:
    """Return the current paper-ingestion runtime collaborators."""
    return svc


def set_services(
    *,
    pdf_processor: PDFProcessor | None | object = _UNSET,
    embedder: Embedder | None | object = _UNSET,
    verifier: QuoteVerifier | None | object = _UNSET,
    sources: dict[str, Any] | None | object = _UNSET,
    openai_client: Any | object = _UNSET,
) -> PaperIngestionServices:
    """Set one or more paper-ingestion runtime collaborators.

    Parameters
    ----------
    pdf_processor, embedder, verifier, sources, openai_client:
        Optional collaborator values. Omitted parameters leave the current
        value unchanged; passing ``None`` explicitly clears that collaborator.
    """
    if pdf_processor is not _UNSET:
        svc.pdf_processor = pdf_processor  # type: ignore[assignment]
    if embedder is not _UNSET:
        svc.embedder = embedder  # type: ignore[assignment]
    if verifier is not _UNSET:
        svc.verifier = verifier  # type: ignore[assignment]
    if sources is not _UNSET:
        svc.sources = sources  # type: ignore[assignment]
    if openai_client is not _UNSET:
        svc.openai_client = openai_client
    return svc


def reset_services() -> PaperIngestionServices:
    """Clear all runtime collaborators and return the reset holder."""
    return set_services(
        pdf_processor=None,
        embedder=None,
        verifier=None,
        sources=None,
        openai_client=None,
    )
