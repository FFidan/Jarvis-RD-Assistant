"""Shared validation contracts for the public unified jobs API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

_MAX_BATCH_PAPER_IDS = 50


class PulseGeneratePayload(BaseModel):
    """Payload for the Pulse generation job."""

    kind: Literal["pulse.generate"]
    now: str | None = None


class PaperProcessPayload(BaseModel):
    """Payload for one paper processing job."""

    kind: Literal["paper.process"]
    paper_id: int
    force: bool = False


class PaperAnalyzePayload(BaseModel):
    """Payload for one paper analysis job."""

    kind: Literal["paper.analyze"]
    paper_id: int
    force: bool = False


class PapersBatchProcessPayload(BaseModel):
    """Payload for bounded paper processing batches."""

    kind: Literal["papers.batch_process"]
    paper_ids: list[int] = Field(..., min_length=1, max_length=_MAX_BATCH_PAPER_IDS)
    force: bool = False


class PapersBatchSummarizePayload(BaseModel):
    """Payload for bounded paper summarization batches."""

    kind: Literal["papers.batch_summarize"]
    paper_ids: list[int] = Field(..., min_length=1, max_length=_MAX_BATCH_PAPER_IDS)


class ExtractionBatchPayload(BaseModel):
    """Payload for bounded extraction batches."""

    kind: Literal["extraction.batch"]
    paper_ids: list[int] = Field(..., min_length=1, max_length=_MAX_BATCH_PAPER_IDS)


class CardGeneratePayload(BaseModel):
    """Payload for a card-generation job."""

    kind: Literal["card.generate"]
    paper_id: int
    deck_id: int
    max_cards: int = 5


class NoopTestPayload(BaseModel):
    """Test-only payload accepted when test jobs are enabled."""

    kind: Literal["noop.test"]
    model_config = {"extra": "allow"}


RESEARCH_PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "pulse.generate": PulseGeneratePayload,
    "paper.process": PaperProcessPayload,
    "paper.analyze": PaperAnalyzePayload,
    "papers.batch_process": PapersBatchProcessPayload,
    "papers.batch_summarize": PapersBatchSummarizePayload,
    "extraction.batch": ExtractionBatchPayload,
    "noop.test": NoopTestPayload,
}
LEARNING_PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "card.generate": CardGeneratePayload,
}
PUBLIC_PAYLOAD_SCHEMAS = RESEARCH_PAYLOAD_SCHEMAS | LEARNING_PAYLOAD_SCHEMAS
RESEARCH_PUBLIC_JOB_KINDS = frozenset(RESEARCH_PAYLOAD_SCHEMAS) - {"noop.test"}
LEARNING_PUBLIC_JOB_KINDS = frozenset(LEARNING_PAYLOAD_SCHEMAS)
PUBLIC_JOB_KINDS = RESEARCH_PUBLIC_JOB_KINDS | LEARNING_PUBLIC_JOB_KINDS


def paper_ids_for_payload(payload: dict[str, Any]) -> int | list[int] | None:
    """Return the paper scope carried by a validated public job payload."""
    paper_id = payload.get("paper_id")
    if isinstance(paper_id, int):
        return paper_id
    paper_ids = payload.get("paper_ids")
    if isinstance(paper_ids, list) and all(isinstance(value, int) for value in paper_ids):
        return paper_ids
    return None


__all__ = [
    "CardGeneratePayload",
    "ExtractionBatchPayload",
    "LEARNING_PAYLOAD_SCHEMAS",
    "LEARNING_PUBLIC_JOB_KINDS",
    "NoopTestPayload",
    "PUBLIC_JOB_KINDS",
    "PUBLIC_PAYLOAD_SCHEMAS",
    "PaperAnalyzePayload",
    "PaperProcessPayload",
    "PapersBatchProcessPayload",
    "PapersBatchSummarizePayload",
    "PulseGeneratePayload",
    "RESEARCH_PAYLOAD_SCHEMAS",
    "RESEARCH_PUBLIC_JOB_KINDS",
    "paper_ids_for_payload",
]
