"""Dashboard aggregate-metrics Pydantic models."""

from typing import Literal

from pydantic import BaseModel


class DashboardMetrics(BaseModel):
    """Aggregate metrics for the dashboard home page."""

    total_papers: int
    unread_papers: int
    pending_papers: int
    due_cards: int
    active_projects: int
    topic_count: int
    nudge_count: int
    chunked_papers: int = 0
    onboarding_stage: Literal["needs_topics", "needs_papers", "needs_processing", "complete"] = (
        "needs_topics"
    )
