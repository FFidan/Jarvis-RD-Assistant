"""Pydantic v2 data models for the Learning Engine Service.

Maps to the PostgreSQL schema defined in db/init.sql and provides
request/response schemas for all API endpoints.
"""

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from jarvis_common import HealthCheckResponse  # noqa: F401 — re-exported

# --- Enums ---

class CardType(str, Enum):
    """Supported flashcard types."""
    CONCEPT = "concept"
    QUOTE = "quote"
    METHOD = "method"
    COMPARISON = "comparison"


class Rating(int, Enum):
    """FSRS review rating."""
    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


# --- Nested Models ---

class Evidence(BaseModel):
    """Evidence linking a card to source text."""
    quote: str | None = None
    page_number: int | None = None
    chunk_id: int | None = None
    snapshot_path: str | None = None
    verified: bool = True

    # Backwards compat — existing JSONB rows may have pdf_snapshot_path
    pdf_snapshot_path: str | None = Field(default=None, exclude=True, deprecated=True)

    @model_validator(mode="before")
    @classmethod
    def _migrate_snapshot(cls, data):
        if isinstance(data, dict) and data.get("pdf_snapshot_path") and not data.get("snapshot_path"):
            data["snapshot_path"] = data.pop("pdf_snapshot_path")
        return data


# --- Request Models ---

class DeckCreate(BaseModel):
    """Request body for creating a deck."""
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    topic_id: int | None = None


class CardCreate(BaseModel):
    """Request body for creating a card."""
    deck_id: int
    card_type: CardType
    front: str = Field(..., min_length=1)
    back: str = Field(..., min_length=1)
    paper_id: int | None = None
    evidence: Evidence | None = None


class CardUpdate(BaseModel):
    """Request body for updating a card (all fields optional)."""
    front: str | None = None
    back: str | None = None
    card_type: CardType | None = None
    evidence: Evidence | None = None


class ReviewRequest(BaseModel):
    """Request body for submitting a review."""
    rating: Rating
    review_duration_ms: int | None = None


class GenerateCardsRequest(BaseModel):
    """Request body for LLM card generation."""
    paper_id: int
    deck_id: int
    max_cards: int = Field(default=5, ge=1, le=20)


class BatchGenerateRequest(BaseModel):
    """Request body for batch card generation across all unprocessed papers."""
    deck_id: int
    max_per_paper: int = Field(default=5, ge=1, le=20)


# --- Response Models ---

class DeckResponse(BaseModel):
    """Deck with computed card counts."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    topic_id: int | None = None
    card_count: int = 0
    due_count: int = 0
    created_at: datetime


class CardResponse(BaseModel):
    """Full card representation returned by the API."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    deck_id: int
    paper_id: int | None = None
    card_type: str
    front: str
    back: str
    evidence: Evidence | None = None
    fsrs_state: dict = Field(default_factory=dict)
    due_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ReviewResponse(BaseModel):
    """Response after submitting a review."""
    card_id: int
    rating: int
    next_due_at: datetime
    fsrs_state: dict
    review_log_id: int


class GenerateCardsResponse(BaseModel):
    """Response from card generation."""
    cards_created: int
    cards: list[CardResponse]
    confidence: str = "MEDIUM"


class RetentionStats(BaseModel):
    """Retention and review statistics."""
    total_cards: int
    due_now: int
    reviewed_today: int
    average_retention: float
    reviews_by_rating: dict[str, int] = Field(default_factory=dict)
    streak_days: int


# --- Project Management Models ---


class ProjectCreate(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    status: Literal["active", "paused", "completed", "archived"] = Field(default="active")
    deadline: date | None = None
    color: str | None = Field(
        default=None,
        max_length=7,
        pattern=r"^#[0-9A-Fa-f]{6}$",
    )


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    status: Literal["active", "paused", "completed", "archived"] | None = None
    deadline: date | None = None
    color: str | None = Field(
        default=None,
        max_length=7,
        pattern=r"^#[0-9A-Fa-f]{6}$",
    )


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None
    status: Literal["active", "paused", "completed", "archived"] = "active"
    deadline: date | None = None
    color: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    title: str = Field(..., max_length=500)
    description: str | None = None
    status: Literal["todo", "in_progress", "done", "blocked"] = Field(default="todo")
    priority: int = Field(default=3, ge=1, le=4)
    deadline: date | None = None
    estimated_hours: float | None = None
    parent_task_id: int | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status: Literal["todo", "in_progress", "done", "blocked"] | None = None
    priority: int | None = Field(default=None, ge=1, le=4)
    deadline: date | None = None
    estimated_hours: float | None = None
    actual_hours: float | None = None
    sort_order: int | None = None


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    parent_task_id: int | None = None
    title: str
    description: str | None = None
    status: Literal["todo", "in_progress", "done", "blocked"] = "todo"
    priority: int = 3
    deadline: date | None = None
    estimated_hours: float | None = None
    actual_hours: float | None = None
    sort_order: int = 0
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MilestoneCreate(BaseModel):
    name: str = Field(..., max_length=255)
    deadline: date | None = None
    description: str | None = None


class MilestoneUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    deadline: date | None = None
    description: str | None = None
    completed: bool | None = None


class MilestoneResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    deadline: date | None = None
    description: str | None = None
    completed: bool = False
    completed_at: datetime | None = None
    created_at: datetime


class TaskPaperLinkCreate(BaseModel):
    paper_id: int
    note: str | None = Field(
        default=None,
        validation_alias=AliasChoices("note", "notes"),
    )


# --- Endpoint Response Models ---


class BatchGenerateResponse(BaseModel):
    """Response for POST /api/generate/batch."""

    papers_processed: int
    cards_created: int
    errors: list[str] = Field(default_factory=list)


class ProjectDetailResponse(BaseModel):
    """Response for GET /api/projects/{project_id} with task/milestone counts."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    status: Literal["active", "paused", "completed", "archived"] = "active"
    deadline: date | None = None
    color: str | None = None
    created_at: datetime
    updated_at: datetime
    total_tasks: int = 0
    done_tasks: int = 0
    total_milestones: int = 0
    completed_milestones: int = 0


class TaskPaperLinkResponse(BaseModel):
    """Response for POST /api/tasks/{task_id}/papers."""

    model_config = ConfigDict(from_attributes=True)

    task_id: int
    paper_id: int
    note: str | None = None


class ProjectPaperLinkResponse(BaseModel):
    """Response for POST /api/projects/{project_id}/papers/{paper_id}."""

    project_id: int
    paper_id: int


# --- Analytics Models ---


class ActivityItem(BaseModel):
    """A single row from GET /api/analytics/activity."""

    log_date: date
    tasks_completed: int
    cards_reviewed: int
    papers_read: int
    focus_hours: float
    notes: str | None = None


class ReviewDistributionItem(BaseModel):
    """A single row from GET /api/analytics/reviews."""

    rating: int
    count: int


class RetentionItem(BaseModel):
    """A single row from GET /api/analytics/retention."""

    review_date: date
    total: int
    good_easy: int
    retention_pct: float | None = None


class LLMCostItem(BaseModel):
    """A single row from GET /api/analytics/llm-cost."""

    day: date
    total_cost: float
    workflow: str


# --- Project Papers Models ---


class ProjectPaperItem(BaseModel):
    """A single paper linked to a project from GET /api/projects/{project_id}/papers."""

    id: int
    title: str
    authors: list[str]
    source_type: str
    published_date: date | None = None
    notes: str | None = None
    added_at: datetime
