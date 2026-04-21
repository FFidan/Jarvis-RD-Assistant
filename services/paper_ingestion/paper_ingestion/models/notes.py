"""Paper-note Pydantic models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NoteCreate(BaseModel):
    """Request body for creating a paper note."""

    user_note: str = Field(..., min_length=1, max_length=5000)
    highlight_text: str | None = None
    page_number: int | None = Field(default=None, ge=1)


class NoteUpdate(BaseModel):
    """Request body for updating a paper note."""

    user_note: str | None = Field(default=None, min_length=1, max_length=5000)
    highlight_text: str | None = None
    page_number: int | None = Field(default=None, ge=1)


class NoteResponse(BaseModel):
    """Response for a paper note."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    user_note: str
    highlight_text: str | None = None
    page_number: int | None = None
    created_at: datetime
