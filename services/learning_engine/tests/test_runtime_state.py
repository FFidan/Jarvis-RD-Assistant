"""Tests for learning-engine runtime collaborator state."""

from __future__ import annotations


def test_set_services_updates_openai_client() -> None:
    """set_services exposes the OpenAI client through get_services."""
    from learning_engine._state import get_services, reset_services, set_services

    reset_services()
    openai_client = object()

    services = set_services(openai_client=openai_client)

    assert services is get_services()
    assert get_services().openai_client is openai_client


def test_reset_services_clears_openai_client() -> None:
    """reset_services clears the job-visible OpenAI client."""
    from learning_engine._state import get_services, reset_services, set_services

    set_services(openai_client=object())

    reset_services()

    assert get_services().openai_client is None
