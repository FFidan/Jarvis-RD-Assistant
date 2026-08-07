"""SSE frame helpers shared between paper_ingestion and learning_engine."""

import json
from typing import Any

SSE_DONE = "data: [DONE]\n\n"


def sse_event(payload: dict[str, Any]) -> str:
    """Format a payload dict as a single SSE 'data:' line + blank line terminator."""
    return f"data: {json.dumps(payload)}\n\n"


def sse_named_event(event: str, payload: dict[str, Any]) -> str:
    """Format a payload dict as a named SSE event: an ``event:`` line, then the data frame.

    Callers needing a named event must use this instead of inlining the frame bytes,
    so the wire format stays defined in one place (``ENGINEERING_STANDARDS.md``).
    """
    return f"event: {event}\n{sse_event(payload)}"


def sse_keepalive() -> str:
    """Return a SSE keepalive comment line."""
    return ": keepalive\n\n"
