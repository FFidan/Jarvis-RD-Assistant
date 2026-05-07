"""SSE frame helpers shared between paper_ingestion and learning_engine."""

import json
from typing import Any

SSE_DONE = "data: [DONE]\n\n"


def sse_event(payload: dict[str, Any]) -> str:
    """Format a payload dict as a single SSE 'data:' line + blank line terminator."""
    return f"data: {json.dumps(payload)}\n\n"


def sse_keepalive() -> str:
    """Return a SSE keepalive comment line."""
    return ": keepalive\n\n"
