"""Tracked-author Pydantic models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TrackedAuthorCreate(BaseModel):
    """Request body for creating a tracked author."""

    author_name: str = Field(..., min_length=1, max_length=500)
    s2_author_id: str | None = Field(default=None, max_length=50)


class TrackedAuthorUpdate(BaseModel):
    """Request body for updating a tracked author."""

    enabled: bool | None = None
    s2_author_id: str | None = Field(default=None, max_length=50)


class TrackedAuthorResponse(BaseModel):
    """Response for a tracked author record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    author_name: str
    s2_author_id: str | None = None
    source: str
    enabled: bool
    last_checked_at: datetime | None = None
    created_at: datetime


class AutoDetectResponse(BaseModel):
    """Response for auto-detect authors endpoint."""

    added: int
    already_tracked: int
    authors: list[TrackedAuthorResponse]


class AuthorAlertMatch(BaseModel):
    """A tracked author and the newly-alerted papers matched for them.

    ``papers`` carries the keys the Telegram bot's ``format_paper_card``
    consumes (``id, title, authors, published_date, source_type, url,
    metadata``); values are NULL-tolerant.
    """

    author_name: str
    papers: list[dict]


class AuthorCheckResponse(BaseModel):
    """Response for check tracked authors endpoint."""

    new_papers: int
    authors_checked: int
    matches: list[AuthorAlertMatch] = []
