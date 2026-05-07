"""SSE wire-format helpers for streaming routes.

Re-exports shared helpers from ``jarvis_common.sse`` so existing import paths
(``from paper_ingestion.routers._sse import SSE_DONE, sse_event``) continue to work.
"""

from typing import Any

import jarvis_common.sse as _sse_mod

# Re-export via assignment so pyright tracks the symbols in this module's namespace.
SSE_DONE: str = _sse_mod.SSE_DONE


def sse_event(payload: dict[str, Any]) -> str:
    """Format a payload dict as a single SSE 'data:' line + blank line terminator."""
    return _sse_mod.sse_event(payload)


def sse_keepalive() -> str:
    """Return a SSE keepalive comment line."""
    return _sse_mod.sse_keepalive()


__all__ = ["SSE_DONE", "sse_event", "sse_keepalive"]
