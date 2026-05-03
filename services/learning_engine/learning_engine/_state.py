"""Module-level holders for service-scoped singletons.

These are set once during FastAPI lifespan startup and read by job handlers
that run in-process but must not import ``learning_engine.main`` (which would
create a circular dependency).

Usage
-----
In ``main.py`` lifespan::

    from learning_engine._state import svc

    svc.openai_client = instructor.from_openai(openai.AsyncOpenAI(...))

In job handlers / helpers::

    from learning_engine._state import svc

    openai_client = svc.openai_client
"""

from __future__ import annotations

from typing import Any


class _ServiceState:
    """Holds references to long-lived service objects populated during lifespan."""

    openai_client: Any | None = None  # Instructor-patched openai.AsyncOpenAI, set in lifespan


svc = _ServiceState()
