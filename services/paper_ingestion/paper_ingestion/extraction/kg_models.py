"""Pydantic output models for knowledge graph entity extraction.

Used by ``call_llm_structured`` (Instructor) in ``extraction/entities.py``
to produce validated, typed KG output instead of raw dicts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class KGEntityCandidate(BaseModel):
    """A candidate knowledge-graph entity extracted from a paper.

    Attributes
    ----------
    name : str
        Canonical entity name (1–200 characters).
    type : str
        Entity category: ``"method"``, ``"dataset"``, ``"metric"``,
        ``"concept"``, ``"institution"``, or ``"author"``.
    description : str | None
        Optional brief description (max 500 characters).
    """

    name: str = Field(min_length=1, max_length=200)
    type: Literal["method", "dataset", "metric", "concept", "institution", "author"]
    description: str | None = Field(default=None, max_length=500)


class KGRelationshipCandidate(BaseModel):
    """A candidate directed relationship between two KG entities.

    Attributes
    ----------
    source : str
        Name of the source entity.
    target : str
        Name of the target entity.
    type : str
        Relationship type: ``"used_on"``, ``"outperforms"``, ``"extends"``,
        ``"evaluates"``, ``"proposes"``, or ``"affiliated_with"``.
    evidence : str
        Verbatim quote from the paper supporting this relationship (min 10 chars).
    confidence : float
        Extractor confidence score (0.0–1.0); defaults to 0.8.
    """

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: Literal["used_on", "outperforms", "extends", "evaluates", "proposes", "affiliated_with"]
    evidence: str = Field(min_length=10, description="Verbatim evidence quote")
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)


class KGExtractionOutput(BaseModel):
    """Structured LLM output for a single paper KG extraction pass.

    Attributes
    ----------
    entities : list[KGEntityCandidate]
        Up to 15 extracted entities.
    relationships : list[KGRelationshipCandidate]
        Up to 10 directed relationships between the extracted entities.
    """

    entities: list[KGEntityCandidate] = Field(default_factory=list, max_length=15)
    relationships: list[KGRelationshipCandidate] = Field(default_factory=list, max_length=10)
