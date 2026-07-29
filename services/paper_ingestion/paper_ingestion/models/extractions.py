"""Structured-extraction + quote-verification Pydantic models."""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from paper_ingestion.models.papers import Confidence

# --- Verification Models ---


class VerificationResult(BaseModel):
    """Result of verifying a single quote against source text."""

    quote: str
    verified: bool
    match_type: str | None = None  # "exact" | "fuzzy" | None
    match_score: float | None = None  # 0.0-1.0, only for fuzzy
    matched_text: str | None = None  # actual text that matched
    chunk_id: int | None = None
    page_number: int | None = None
    matched_span_start: int | None = None  # byte offset of matched_text in full_text (O(1) lookup)


class VerificationReport(BaseModel):
    """Aggregate verification results for a full summary."""

    total_findings: int
    verified_count: int
    failed_count: int
    pass_rate: float  # verified_count / total_findings
    confidence: Confidence
    results: list[VerificationResult]


# --- Structured Extraction Models ---


class ExtractionField(BaseModel):
    """A single field definition within an extraction template."""

    name: str
    label: str
    description: str
    type: str = "text"  # text, number, list

    @field_validator("name")
    @classmethod
    def name_must_be_identifier(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", v):
            raise ValueError(f"Template field name '{v}' must be a valid Python identifier")
        return v


class ExtractionTemplateCreate(BaseModel):
    """Request body for creating an extraction template."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    fields: list[ExtractionField] = Field(..., min_length=1)
    is_default: bool = False


class ExtractionTemplateUpdate(BaseModel):
    """Request body for updating an extraction template."""

    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    fields: list[ExtractionField] | None = None
    is_default: bool | None = None


class ExtractionTemplateResponse(BaseModel):
    """Response for an extraction template."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    fields: list[ExtractionField]
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class ExtractedField(BaseModel):
    """A single extracted field value with evidence."""

    value: Any = None
    quote: str | None = None
    verified: bool = False
    confidence: float = 0.0
    chunk_id: int | None = None
    page_number: int | None = None


class ExtractionResponse(BaseModel):
    """Response for a paper extraction."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    template_id: int
    extractions: dict[str, ExtractedField]
    extraction_model: str | None = None
    content_generation: int = 0
    created_at: datetime


class ExtractionRequest(BaseModel):
    """Request body for extracting fields from a paper."""

    template_id: int


class BatchExtractionRequest(BaseModel):
    """Request body for batch extraction."""

    paper_ids: list[int] = Field(..., min_length=1, max_length=50)
    template_id: int


class BatchExtractionResponse(BaseModel):
    """Response for batch extraction."""

    extracted: int
    failed: int
    skipped: int
    total: int
    remaining: int = 0
    status: Literal["ok", "partial", "cancelled"]


class ExtractionTableRow(BaseModel):
    """A row in the cross-paper extraction table."""

    paper_id: int
    paper_title: str
    extractions: dict[str, ExtractedField]


class BatchEntityExtractResponse(BaseModel):
    """Response for POST /api/knowledge-graph/extract-entities/batch."""

    extracted: int
    failed: int
    total: int
