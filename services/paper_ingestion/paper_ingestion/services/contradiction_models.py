"""Pydantic models for structured LLM output in contradiction detection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ContradictionClassification(BaseModel):
    is_contradiction: bool
    contradiction_type: Literal["direct", "methodological", "result", "interpretation"] = "direct"
    explanation: str = Field(min_length=10, max_length=400)
    quote_a: str = Field(default="")
    quote_b: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _quotes_required_if_contradiction(self) -> ContradictionClassification:
        if self.is_contradiction and (not self.quote_a.strip() or not self.quote_b.strip()):
            raise ValueError("is_contradiction=True requires non-empty quote_a and quote_b")
        return self
