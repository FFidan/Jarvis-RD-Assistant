"""Pydantic v2 data models for the Learning Engine Service.

Maps to the PostgreSQL schema defined in db/init.sql and provides
request/response schemas for all API endpoints.
"""

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

# --- Enums ---


class CardType(str, Enum):  # noqa: UP042 — keep str+Enum for Pydantic v2 compat
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
        if (
            isinstance(data, dict)
            and data.get("pdf_snapshot_path")
            and not data.get("snapshot_path")
        ):
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
    front: str = Field(..., min_length=1, max_length=500)
    back: str = Field(..., min_length=1, max_length=2000)
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
    review_duration_ms: int | None = Field(default=None, ge=0)


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
    card_type: CardType
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


class ReviewSyncEvent(BaseModel):
    """One offline review replayed from the client outbox (contract 2026-05-16)."""

    idempotency_key: str = Field(..., min_length=1, max_length=128)
    card_id: int
    rating: Rating
    reviewed_at: datetime
    review_duration_ms: int | None = Field(default=None, ge=0)


class ReviewSyncRequest(BaseModel):
    """Batch of offline reviews to reconcile, oldest-first."""

    reviews: list[ReviewSyncEvent] = Field(..., max_length=500)


class ReviewSyncResponse(BaseModel):
    """Reconcile outcome: durably-recorded vs declined event counts.

    Fields:
    - synced: events newly written to review_logs in this call.
    - skipped: events rejected (card not found / not owned by caller).
    - already_synced: events skipped because their idempotency_key was
      already present in review_logs (idempotency fast-path or concurrent
      ON CONFLICT race). Not counted as ``synced`` to avoid inflating the
      caller's perceived write throughput.
    """

    synced: int = Field(..., ge=0)
    skipped: int = Field(..., ge=0)
    already_synced: int = Field(default=0, ge=0)


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
    """Request body for POST /api/projects."""

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
    """Request body for PUT /api/projects/{project_id} (all fields optional)."""

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
    """Project summary returned by list and create endpoints.

    ``paper_count`` and ``open_question_count`` are LEFT JOIN aggregations
    computed at query time; both default to 0 when nothing is linked.
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None
    status: Literal["active", "paused", "completed", "archived"] = "active"
    deadline: date | None = None
    color: str | None = None
    created_at: datetime
    updated_at: datetime
    # Projects IA redesign §3.6/§4c — chapter-rail row counts. LEFT JOIN
    # aggregations in list_projects; 0 when nothing linked.
    paper_count: int = 0
    open_question_count: int = 0


class ProjectQuestionCreate(BaseModel):
    """Request body for POST /api/projects/{project_id}/questions."""

    body: str = Field(..., min_length=1, max_length=2000)


class ProjectQuestionResponse(BaseModel):
    """A single open research question (Projects § OPEN QUESTIONS)."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    body: str
    created_at: datetime


class ProjectActivityItem(BaseModel):
    """A single row from GET /api/projects/{project_id}/activity (UNION feed)."""

    model_config = ConfigDict(from_attributes=True)
    kind: Literal["added_paper", "completed_task", "completed_milestone"]
    ts: datetime
    label: str


class TaskCreate(BaseModel):
    """Request body for POST /api/projects/{project_id}/tasks."""

    title: str = Field(..., max_length=500)
    description: str | None = None
    status: Literal["todo", "in_progress", "done", "blocked"] = Field(default="todo")
    priority: int = Field(default=3, ge=1, le=4)
    deadline: date | None = None
    estimated_hours: float | None = None
    parent_task_id: int | None = None


class TaskUpdate(BaseModel):
    """Request body for PUT /api/tasks/{task_id} (all fields optional)."""

    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    status: Literal["todo", "in_progress", "done", "blocked"] | None = None
    priority: int | None = Field(default=None, ge=1, le=4)
    deadline: date | None = None
    estimated_hours: float | None = None
    actual_hours: float | None = None
    sort_order: int | None = None


class TaskResponse(BaseModel):
    """Task returned by list, create, and update endpoints.

    ``deadline`` is stored as TIMESTAMPTZ; the frontend receives an
    ISO-8601 datetime string.  ``completed_at`` is set automatically
    when status transitions to ``done``.
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int | None = None
    parent_task_id: int | None = None
    title: str
    description: str | None = None
    status: Literal["todo", "in_progress", "done", "blocked"] = "todo"
    priority: int = 3
    # DB column is TIMESTAMPTZ; frontend receives ISO-8601 datetime string
    deadline: datetime | None = None
    estimated_hours: float | None = None
    actual_hours: float | None = None
    sort_order: int = 0
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MilestoneCreate(BaseModel):
    """Request body for POST /api/projects/{project_id}/milestones."""

    name: str = Field(..., max_length=255)
    deadline: datetime = Field(...)
    description: str | None = None


class MilestoneUpdate(BaseModel):
    """Request body for PUT /api/milestones/{milestone_id} (all fields optional)."""

    name: str | None = Field(default=None, max_length=255)
    deadline: datetime | None = None  # DB column is TIMESTAMPTZ
    description: str | None = None
    completed: bool | None = None


class MilestoneResponse(BaseModel):
    """Milestone returned by list, create, and update endpoints.

    ``completed_at`` is set automatically when ``completed`` transitions to
    ``True`` and cleared when it transitions back to ``False``.
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    deadline: datetime | None = None
    description: str | None = None
    completed: bool = False
    completed_at: datetime | None = None
    created_at: datetime


class TaskPaperLinkCreate(BaseModel):
    """Request body for POST /api/tasks/{task_id}/papers."""

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


class BatchAcceptedResponse(BaseModel):
    """202 Accepted response for POST /api/generate/batch."""

    job_id: str
    status: str = "pending"


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
    # Projects IA redesign §4c — parity with the widened list response.
    paper_count: int = 0
    open_question_count: int = 0


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


# --- Executive Endpoint Response Models ---


class FocusSessionResponse(BaseModel):
    """Response for POST /api/executive/focus/log."""

    status: str
    recorded_hours: float


class MyDayTaskItem(BaseModel):
    """A single task entry in the my-day response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None = None
    title: str
    priority: int
    # DB column is TIMESTAMPTZ; frontend receives ISO-8601 datetime string
    deadline: datetime | None = None
    status: str
    completed_at: datetime | None = None
    project_name: str | None = None
    project_color: str | None = None


class MyDayRecommendationItem(BaseModel):
    """A single recommendation entry in the my-day response."""

    model_config = ConfigDict(from_attributes=True)

    recommendation_id: int
    paper_id: int
    score: float
    title: str
    authors: list[str]


class MyDayProjectPulseItem(BaseModel):
    """A single active-project entry in the my-day response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    color: str | None = None
    total_tasks: int
    done_tasks: int
    next_milestone: str | None = None
    next_milestone_deadline: date | None = None


class MyDayResponse(BaseModel):
    """Response for GET /api/executive/my-day."""

    tasks: list[MyDayTaskItem]
    cards_due: int
    recommendations: list[MyDayRecommendationItem]
    today_focus_hours: float
    focus_streak_days: int
    project_pulse: list[MyDayProjectPulseItem]


# --- Analytics Summary Model ---


class AnalyticsSummaryResponse(BaseModel):
    """Response for GET /api/analytics/summary.

    Returns current-period and prior-period totals from daily_log plus
    consecutive-day streaks for focus and cards review.
    """

    # Current-period totals (SUM over [today - days, today))
    papers_read_total: int
    focus_hours_total: float
    cards_reviewed_total: int

    # Prior-period totals (SUM over [today - 2*days, today - days))
    papers_read_prev: int
    focus_hours_prev: float
    cards_reviewed_prev: int

    # Consecutive-day streaks
    focus_streak_days: int  # consecutive days with focus_hours > 0
    cards_review_streak_days: int  # consecutive days with cards_reviewed > 0
