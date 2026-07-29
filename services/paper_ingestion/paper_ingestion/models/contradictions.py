"""Models for verified cross-paper contradiction detection."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ContradictionStatus = Literal["verified", "dismissed", "false_positive"]
ContradictionType = Literal["direct", "methodological", "result", "interpretation"]


class ContradictionScanRequest(BaseModel):
    """Request body for enqueuing a contradiction scan."""

    paper_id: int | None = Field(default=None, ge=1)
    limit: int = Field(default=25, ge=1, le=100)


class PaperContradictionResponse(BaseModel):
    """A quote-verified contradiction between two paper findings."""

    id: int
    paper_a_id: int
    paper_b_id: int
    paper_a_title: str
    paper_b_title: str
    finding_a: str
    finding_b: str
    quote_a: str
    quote_b: str
    page_a: int | None = None
    page_b: int | None = None
    contradiction_type: ContradictionType
    explanation: str
    confidence: float
    status: ContradictionStatus
    created_at: datetime


class ContradictionListResponse(BaseModel):
    """List response for contradiction queries."""

    contradictions: list[PaperContradictionResponse]
    total: int


class ConsensusAssessment(BaseModel):
    """One verified agreement/disagreement underlying a consensus claim."""

    stance: str
    paper_a_title: str
    paper_b_title: str
    quote_a: str
    quote_b: str
    page_a: int | None = None
    page_b: int | None = None


class ConsensusClaim(BaseModel):
    """Agreement/disagreement among related papers on one shared claim."""

    claim_topic: str
    supports: int
    opposes: int
    paper_ids: list[int]
    assessments: list[ConsensusAssessment]


class ConsensusResponse(BaseModel):
    """Consensus view: stance counts per shared claim across the library.

    ``truncated`` is ``True`` when the underlying verified evidence set exceeded
    the internal row cap before clustering began -- independent of ``total``,
    which counts the returned clusters and keeps its existing meaning.
    """

    claims: list[ConsensusClaim]
    total: int
    truncated: bool = False
