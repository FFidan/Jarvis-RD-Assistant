"""Tests for paper-ingestion runtime collaborator state."""

from __future__ import annotations


def test_set_services_updates_only_supplied_values() -> None:
    """set_services leaves omitted collaborators unchanged."""
    from paper_ingestion._state import get_services, reset_services, set_services

    reset_services()
    pdf_processor = object()
    embedder = object()

    first = set_services(pdf_processor=pdf_processor, embedder=embedder)
    second = set_services(verifier=None)

    assert first is second
    services = get_services()
    assert services.pdf_processor is pdf_processor
    assert services.embedder is embedder
    assert services.verifier is None


def test_reset_services_clears_all_collaborators() -> None:
    """reset_services clears values that job handlers read through get_services."""
    from paper_ingestion._state import get_services, reset_services, set_services

    set_services(
        pdf_processor=object(),
        embedder=object(),
        verifier=object(),
        sources={"semantic_scholar": object()},
        openai_client=object(),
    )

    reset_services()

    services = get_services()
    assert services.pdf_processor is None
    assert services.embedder is None
    assert services.verifier is None
    assert services.sources is None
    assert services.openai_client is None
