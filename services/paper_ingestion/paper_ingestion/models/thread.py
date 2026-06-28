"""Pydantic models for the `thread` entity (My-Day Open threads).

A ``thread`` is a user's resumable mid-flight line of work surfaced in My-Day's
§ Open threads section and the 3-mode hero ("Resume thread"). It is both
user-created AND auto-seeded from (a) interrupted Pomodoro sessions and (b) the
EOD "make this a thread" action.

Note: ``from __future__ import annotations`` is intentionally absent — the same
PydanticUserError-with-FastAPI-body trace documented in ``routers/my_day.py``
applies to any model used as a request body. Concrete annotations only.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ThreadCreate(BaseModel):
    """Request body for the manual create path (POST /api/my-day/threads)."""

    title: str = Field(..., min_length=1, max_length=500)
    anchor: str | None = Field(default=None, max_length=2000)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)


class ThreadUpdate(BaseModel):
    """Partial update body (PATCH /api/my-day/threads/{id}).

    Every field optional; only provided fields are written. ``progress`` and
    ``status`` are the resume/update-progress affordances from the prototype.
    """

    title: str | None = Field(default=None, min_length=1, max_length=500)
    anchor: str | None = Field(default=None, max_length=2000)
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str | None = Field(default=None, pattern="^(open|done|archived)$")


class ThreadResponse(BaseModel):
    id: int
    title: str
    anchor: str | None
    progress: float
    last_at: datetime
    status: str
    created_at: datetime


class ThreadSeedResponse(BaseModel):
    """Returned by the two auto-seed producers (Pomodoro / EOD).

    ``created`` is False when an equivalent open thread already existed and was
    touched (last_at bumped) instead of duplicated.
    """

    thread: ThreadResponse
    created: bool
