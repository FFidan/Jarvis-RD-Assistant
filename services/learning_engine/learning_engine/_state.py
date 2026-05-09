"""Module-level holders for service-scoped singletons.

These are set once during FastAPI lifespan startup and read by job handlers
that run in-process but must not import ``learning_engine.main`` (which would
create a circular dependency).

Usage
-----
In ``main.py`` lifespan::

    from learning_engine._state import set_services

    set_services(openai_client=instructor.from_openai(openai.AsyncOpenAI(...)))

In job handlers / helpers::

    from learning_engine._state import get_services

    openai_client = get_services().openai_client
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_UNSET = object()


@dataclass(slots=True)
class LearningEngineServices:
    """Long-lived learning-engine collaborators populated during lifespan.

    Attributes
    ----------
    openai_client:
        Instructor-patched OpenAI client configured for structured card
        generation calls.
    """

    openai_client: Any | None = None  # Instructor-patched openai.AsyncOpenAI, set in lifespan


svc = LearningEngineServices()


def get_services() -> LearningEngineServices:
    """Return the current learning-engine runtime collaborators."""
    return svc


def set_services(*, openai_client: Any | object = _UNSET) -> LearningEngineServices:
    """Set one or more learning-engine runtime collaborators.

    Parameters
    ----------
    openai_client:
        Optional Instructor-patched OpenAI client. Omitted means unchanged;
        passing ``None`` explicitly clears the collaborator.
    """
    if openai_client is not _UNSET:
        svc.openai_client = openai_client
    return svc


def reset_services() -> LearningEngineServices:
    """Clear all runtime collaborators and return the reset holder."""
    return set_services(openai_client=None)
