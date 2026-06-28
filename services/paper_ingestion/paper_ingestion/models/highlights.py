"""Spatial PDF-highlight Pydantic models (in-PDF annotation reader)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Rect(BaseModel):
    """A normalized, top-origin rectangle on a PDF page (coordinates in [0, 1])."""

    x0: float = Field(..., ge=0.0, le=1.0)
    y0: float = Field(..., ge=0.0, le=1.0)
    x1: float = Field(..., ge=0.0, le=1.0)
    y1: float = Field(..., ge=0.0, le=1.0)


class HighlightRect(BaseModel):
    """Highlight geometry: the union box plus its per-line rectangles.

    Field names are camelCase to match the react-pdf-highlighter wire shape that
    the frontend sends and that is stored verbatim in the ``rect`` JSONB column.
    """

    boundingRect: Rect  # noqa: N815 - external (FE/JSONB) wire-shape key
    rects: list[Rect]


class HighlightCreate(BaseModel):
    """Request body for creating a highlight."""

    page: int = Field(..., ge=1)
    rect: HighlightRect
    note: str | None = Field(default=None, max_length=5000)
    color: str | None = Field(default=None, max_length=32)
    quote: str | None = Field(default=None, max_length=10000)


class HighlightUpdate(BaseModel):
    """Request body for updating a highlight's note and/or color."""

    note: str | None = Field(default=None, max_length=5000)
    color: str | None = Field(default=None, max_length=32)


class HighlightResponse(BaseModel):
    """Response for a stored highlight."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    page: int
    rect: HighlightRect
    note: str | None = None
    color: str | None = None
    quote: str | None = None
    created_at: datetime
