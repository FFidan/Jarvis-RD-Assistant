"""Pydantic output models for the Pulse scoring pipeline."""

from pydantic import BaseModel, Field


class PulseScoringOutput(BaseModel):
    """Structured output from the LLM stage-2 scoring call."""

    relevance: int = Field(ge=1, le=10)
    novelty: int = Field(ge=1, le=10)
    reasoning: str = Field(min_length=1, max_length=400)
