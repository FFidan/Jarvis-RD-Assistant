"""Topic, nudge, and global-config Pydantic models.

Models tied to the ``topics``, ``nudges``, and ``user_config`` tables.
``TopicRef`` is the lightweight handle passed into source-plugin polling.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TopicRef(BaseModel):
    """Lightweight topic reference passed to source polling methods.

    Used by PaperSource.fetch_new_since() so sources can filter by topic
    without a round-trip to the database.
    """

    id: int
    name: str
    description: str | None = None
    query_terms: list[str] = []


class TopicCreate(BaseModel):
    name: str = Field(..., max_length=255)
    query_terms: list[str] = Field(..., min_length=1)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool = True

    @field_validator("query_terms")
    @classmethod
    def validate_query_terms(cls, value: list[str]) -> list[str]:
        cleaned = [term.strip() for term in value]
        if not all(cleaned):
            raise ValueError("query_terms must not contain blank strings")
        return cleaned


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    query_terms: list[str] | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None

    @field_validator("query_terms")
    @classmethod
    def validate_query_terms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = [term.strip() for term in value]
        if not all(cleaned):
            raise ValueError("query_terms must not contain blank strings")
        return cleaned


class TopicResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    query_terms: list[str]
    category: str | None = None
    description: str | None = None
    enabled: bool = True
    created_at: datetime


# --- Settings / Nudges ---


class ConfigEntry(BaseModel):
    key: str
    value: Any  # JSONB values


class NudgeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    nudge_type: str
    cron_expression: str
    enabled: bool
    config: dict = Field(default_factory=dict)
    last_fired_at: datetime | None = None
    created_at: datetime


class NudgeUpdate(BaseModel):
    cron_expression: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    config: dict | None = None
