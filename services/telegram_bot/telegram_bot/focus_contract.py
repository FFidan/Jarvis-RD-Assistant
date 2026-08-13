"""Validated Telegram view of the Learning Engine focus-session API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FocusSession(BaseModel):
    """One server-authoritative focus interval consumed by Telegram."""

    model_config = ConfigDict(extra="ignore", strict=True)

    id: int
    state: Literal["active", "paused", "completed"]
    source: Literal["web", "telegram"]
    duration_seconds: int = Field(ge=60, le=28_800)
    remaining_seconds: int = Field(ge=0)
    started_at: str = Field(min_length=1)
    paused_at: str | None
    paused_seconds: float = Field(ge=0)
    completed_at: str | None
    recorded_seconds: float = Field(ge=0)
    task_id: int | None
    paper_id: int | None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        for name, value in (
            ("started_at", self.started_at),
            ("paused_at", self.paused_at),
            ("completed_at", self.completed_at),
        ):
            if value is None:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"{name} must be an ISO timestamp") from None
            if parsed.tzinfo is None:
                raise ValueError(f"{name} must include a timezone")
        if (self.state == "paused") != (self.paused_at is not None):
            raise ValueError("paused state and paused_at must agree")
        if (self.state == "completed") != (self.completed_at is not None):
            raise ValueError("completed state and completed_at must agree")
        if self.remaining_seconds > self.duration_seconds:
            raise ValueError("remaining_seconds cannot exceed duration_seconds")
        if self.recorded_seconds > self.duration_seconds:
            raise ValueError("recorded_seconds cannot exceed duration_seconds")
        return self


class FocusTransition(BaseModel):
    """Result of an idempotent focus-session state transition."""

    model_config = ConfigDict(extra="ignore", strict=True)

    session: FocusSession
    changed: bool
