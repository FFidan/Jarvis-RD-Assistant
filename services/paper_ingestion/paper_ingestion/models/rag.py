"""RAG / ask-endpoint Pydantic models."""

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """Request body for conversational RAG on a paper."""

    question: str = Field(..., min_length=1, max_length=2000)
    max_chunks: int = Field(default=5, ge=1, le=10)


class CrossPaperAskRequest(BaseModel):
    """Request body for cross-paper RAG queries."""

    question: str = Field(..., min_length=1, max_length=1000)
    max_chunks: int = Field(default=10, ge=1, le=20)
    max_papers: int = Field(default=5, ge=1, le=15)
    decompose: bool = Field(default=True)


class AskSourceItem(BaseModel):
    """A single source item in an ask response."""

    content: str | None = None
    page_number: int | None = None
    score: float | None = None
    paper_id: int | None = None
    paper_title: str | None = None
    chunk_id: int | None = None


class AskResponse(BaseModel):
    """Response for POST /api/papers/{paper_id}/ask and POST /api/ask."""

    answer: str
    sources: list[AskSourceItem] = Field(default_factory=list)
