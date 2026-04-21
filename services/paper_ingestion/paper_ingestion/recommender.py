"""Back-compat shim — re-exports everything from paper_ingestion.ingestion.recommender.

Existing imports like ``from paper_ingestion.recommender import refresh_recommendations``
continue to work unchanged.  New code should import from the canonical location:
    from paper_ingestion.ingestion.recommender import refresh_recommendations
"""

from paper_ingestion.ingestion.recommender import (  # noqa: F401
    _DEFAULT_LIKED_WEIGHT,
    _DEFAULT_PROJECT_WEIGHT,
    _MAX_RECOMMENDATIONS,
    _MIN_SCORE,
    _aggregate_to_papers,
    _compute_score,
    _filter_unread,
    _get_starred_ids,
    _read_weights,
    _safe_float,
    refresh_recommendations,
)
