"""Canonical location shim — implementation at paper_ingestion.streaming."""

from paper_ingestion.streaming import (
    _SEARCH_SCORE_THRESHOLD,
    CrossPaperRagNoResults,
    CrossPaperRagPrep,
    prepare_cross_paper_rag,
    prepare_single_paper_rag,
    sse_error_stream,
    stream_rag_events,
)

__all__ = [
    "CrossPaperRagNoResults",
    "CrossPaperRagPrep",
    "_SEARCH_SCORE_THRESHOLD",
    "prepare_cross_paper_rag",
    "prepare_single_paper_rag",
    "sse_error_stream",
    "stream_rag_events",
]
