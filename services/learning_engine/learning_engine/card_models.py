"""Pydantic output models for LLM-powered flashcard generation.

Used by ``call_llm_structured`` (Instructor) so that validation errors
are caught at the LLM boundary rather than buried in downstream dict-access.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CardOutput(BaseModel):
    card_type: Literal["concept", "quote", "method", "comparison"]
    front: str = Field(min_length=10, max_length=500)
    back: str = Field(min_length=5, max_length=2000)
    evidence_quote: str = Field(min_length=20)
    page_number: int | None = Field(default=None, ge=1)


class CardGenerationOutput(BaseModel):
    cards: list[CardOutput] = Field(min_length=1, max_length=20)
