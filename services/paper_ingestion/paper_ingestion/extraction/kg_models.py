"""Pydantic output models for knowledge graph entity extraction.

Used by ``call_llm_structured`` (Instructor) in ``extraction/entities.py``
to produce validated, typed KG output instead of raw dicts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class KGEntityCandidate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["method", "dataset", "metric", "concept", "institution", "author"]
    description: str | None = Field(default=None, max_length=500)


class KGRelationshipCandidate(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: Literal["used_on", "outperforms", "extends", "evaluates", "proposes", "affiliated_with"]
    evidence: str = Field(min_length=10, description="Verbatim evidence quote")
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)


class KGExtractionOutput(BaseModel):
    entities: list[KGEntityCandidate] = Field(default_factory=list, max_length=15)
    relationships: list[KGRelationshipCandidate] = Field(default_factory=list, max_length=10)
