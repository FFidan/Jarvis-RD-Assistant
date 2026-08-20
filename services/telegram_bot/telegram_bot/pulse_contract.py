"""Validated Telegram view of the paper-ingestion Pulse response."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PulseCard(BaseModel):
    """Fields consumed by Telegram for one ranked Pulse card."""

    model_config = ConfigDict(extra="ignore", strict=True)

    card_id: int
    paper_id: int
    paper_title: str
    paper_authors: list[str]
    paper_url: str | None
    rank: int = Field(ge=1)
    score: float
    llm_relevance: int | None
    llm_novelty: int | None
    reasoning: str | None
    signals: dict[str, float]
    reasoning_verified: bool | None = None
    reasoning_confidence: Literal["HIGH", "MEDIUM", "LOW", "UNVERIFIED"] | None = None
    #: Server-owned lifecycle state of the card's paper for the requesting user.
    #: ``None`` means no state row exists yet, which the backend reads as the
    #: ``inbox`` default. This is the only record of whether the user has
    #: already acted on a card; Telegram keeps no memory of its own.
    user_state: str | None = None


class PulseDeck(BaseModel):
    """Additive but strict contract for the Pulse truth states Telegram displays."""

    model_config = ConfigDict(extra="ignore", strict=True)

    deck_id: int
    deck_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    card_count: int = Field(ge=0)
    generated_at: str = Field(min_length=1)
    cards: list[PulseCard]
    degraded_reason: str | None = None
    is_stale: bool = False
    stale_age_days: int | None = Field(default=None, ge=0)
    empty_reason: Literal["no_data_yet"] | None = None

    @model_validator(mode="after")
    def validate_truth_state(self) -> Self:
        try:
            date.fromisoformat(self.deck_date)
        except ValueError:
            raise ValueError("deck_date must be a real calendar date") from None
        try:
            generated_at = datetime.fromisoformat(self.generated_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("generated_at must be an ISO timestamp") from None
        if generated_at.tzinfo is None:
            raise ValueError("generated_at must include a timezone")
        if self.card_count != len(self.cards):
            raise ValueError("card_count must match the returned cards")
        if self.is_stale and self.stale_age_days is None:
            raise ValueError("stale decks require stale_age_days")
        if not self.is_stale and self.stale_age_days is not None:
            raise ValueError("current decks cannot carry stale_age_days")
        if self.empty_reason is not None and self.cards:
            raise ValueError("empty_reason cannot accompany cards")
        return self


class PulseGenerateJob(BaseModel):
    """Job identity returned when Telegram starts Pulse generation."""

    model_config = ConfigDict(extra="ignore", strict=True)

    job_id: str = Field(min_length=1, max_length=200)
    status: Literal["queued"]


class PulseGenerateStatus(BaseModel):
    """Progress of a Pulse generation job Telegram is waiting on."""

    model_config = ConfigDict(extra="ignore", strict=True)

    job_id: str = Field(min_length=1, max_length=200)
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]

    @property
    def is_terminal(self) -> bool:
        """Whether the job has stopped changing state."""
        return self.status in {"succeeded", "failed", "cancelled"}
