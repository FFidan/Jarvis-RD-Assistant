"""Back-compat shim — helpers now live in paper_ingestion.services.paper_state_helpers."""

from paper_ingestion.services.paper_state_helpers import (  # noqa: F401
    _upsert_recommendation_feedback,
    _upsert_state_and_starred,
)

__all__ = ["_upsert_recommendation_feedback", "_upsert_state_and_starred"]
