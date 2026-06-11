"""RAG / ask-endpoint Pydantic models."""

from typing import Literal

from pydantic import BaseModel, Field


class HistoryTurn(BaseModel):
    """One prior chat turn; content is treated as DATA, never instructions."""

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class AskRequest(BaseModel):
    """Request body for conversational RAG on a paper."""

    question: str = Field(..., min_length=1, max_length=2000)
    max_chunks: int = Field(default=5, ge=1, le=10)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)


class CrossPaperAskRequest(BaseModel):
    """Request body for cross-paper RAG queries."""

    question: str = Field(..., min_length=1, max_length=1000)
    max_chunks: int = Field(default=10, ge=1, le=20)
    max_papers: int = Field(default=5, ge=1, le=15)
    decompose: bool = Field(default=True)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)


class AskSourceItem(BaseModel):
    """A single source item in an ask response."""

    content: str | None = None
    page_number: int | None = None
    score: float | None = None
    paper_id: int | None = None
    paper_title: str | None = None
    chunk_id: int | None = None


class AskVerifiedSentence(BaseModel):
    """Sentence-level RAG verification result."""

    text: str
    verified: bool


class AskResponse(BaseModel):
    """Response for POST /api/papers/{paper_id}/ask and POST /api/ask."""

    answer: str
    sources: list[AskSourceItem] = Field(default_factory=list)
    confidence: str | None = None  # RagConfidence.value or None
    verified_fraction: float | None = None
    per_sentence: list[AskVerifiedSentence] = Field(default_factory=list)
