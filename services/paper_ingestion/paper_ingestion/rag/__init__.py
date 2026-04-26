"""RAG subpackage — query decomposition, streaming RAG event generation, and verification."""

from paper_ingestion.rag.decomposition import decompose_query
from paper_ingestion.rag.streaming import (
    CrossPaperRagNoResults,
    CrossPaperRagPrep,
    prepare_cross_paper_rag,
    prepare_single_paper_rag,
    sse_error_stream,
    stream_rag_events,
)
from paper_ingestion.rag.verification import (
    RagConfidence,
    RagVerificationReport,
    VerifiedSentence,
    verify_answer_sentences,
)

__all__ = [
    "CrossPaperRagNoResults",
    "CrossPaperRagPrep",
    "RagConfidence",
    "RagVerificationReport",
    "VerifiedSentence",
    "decompose_query",
    "prepare_cross_paper_rag",
    "prepare_single_paper_rag",
    "sse_error_stream",
    "stream_rag_events",
    "verify_answer_sentences",
]
