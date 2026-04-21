"""Pulse deck, card, rating, and debug Pydantic models."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class PulseCardResponse(BaseModel):
    """A single scored card within a Pulse deck."""

    card_id: int
    paper_id: int
    paper_title: str
    paper_authors: list[str]
    paper_url: str | None
    rank: int
    score: float
    llm_relevance: int | None
    llm_novelty: int | None
    reasoning: str | None
    signals: dict[str, float]


class PulseDeckResponse(BaseModel):
    """A full Pulse deck for one day, including all scored cards."""

    deck_id: int
    deck_date: date
    card_count: int
    generated_at: datetime
    cards: list[PulseCardResponse]
    stats: dict
    degraded_reason: str | None = None


class PulseGenerateResponse(BaseModel):
    """Response for POST /api/pulse/generate — returns job_id immediately."""

    job_id: str
    status: str


class PulseStatsResponse(BaseModel):
    """Aggregate Pulse pipeline stats over a sliding window of past runs."""

    window_days: int
    decks_generated: int
    avg_candidates: float | None
    avg_llm_calls: float | None
    avg_duration_s: float | None
    last_run_at: datetime | None
    last_error: str | None
    degraded_reason: str | None = None


class PulseRateRequest(BaseModel):
    """Body for POST /api/pulse/rate."""

    paper_id: int
    rating: Literal["up", "down", "save", "dismiss", "open"]


class PulseRateResponse(BaseModel):
    """Response for POST /api/pulse/rate."""

    status: str


class PulseExplainResponse(BaseModel):
    """Reasoning + signal breakdown for a single Pulse card."""

    card_id: int
    reasoning: str | None = None
    signals: dict = {}
    llm_relevance: float | None = None
    llm_novelty: float | None = None


class PulseDebugTopicEmbedding(BaseModel):
    key: str
    dim: int | None = None
    ok: bool
    non_null: bool


class PulseDebugTopCard(BaseModel):
    card_id: int
    paper_id: int
    title: str | None = None
    signals: dict = {}
    final_score: float
    llm_relevance: float | None = None
    llm_novelty: float | None = None


class PulseDebugResponse(BaseModel):
    """Diagnostics payload for the latest Pulse deck (GET /api/pulse/debug)."""

    deck_date: str
    card_count: int
    degraded_reason: str | None = None
    source_counts: dict = {}
    topic_embeddings: list[PulseDebugTopicEmbedding] = []
    top_cards: list[PulseDebugTopCard] = []
