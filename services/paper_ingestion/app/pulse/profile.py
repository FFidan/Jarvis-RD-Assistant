"""UserProfile model and load_profile() for the Pulse scoring pipeline.

Aggregates all per-user context needed by the three-stage scorer:
topics, tracked authors, library centroid, config weights, and rating history.
"""

import logging
from typing import Any

from pydantic import BaseModel

from app.models import TopicRef

logger = logging.getLogger(__name__)

# Sensible defaults mirroring migration 018 seed values
_DEFAULT_DECK_SIZE = 10
_DEFAULT_STAGE2_TOP_K = 50
_DEFAULT_WEIGHTS: dict[str, float] = {
    "embedding": 0.2,
    "topic": 0.2,
    "llm_relevance": 0.3,
    "llm_novelty": 0.1,
    "author_bonus": 0.15,
    "recency": 0.05,
}
_RATING_HISTORY_LIMIT = 10


class UserProfile(BaseModel):
    """Snapshot of user context consumed by the Pulse scoring pipeline."""

    topics: list[TopicRef]
    tracked_author_ids: list[str]  # author names (and s2_author_ids where present)
    library_centroid: list[float] | None
    weights: dict[str, float]
    deck_size: int
    stage2_top_k: int
    recent_positive_titles: list[str]
    recent_negative_titles: list[str]


async def load_profile(db_pool: Any, *, embedder: Any) -> UserProfile:
    """Load all user context from the database and compute the library centroid.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.
    embedder:
        Embedder instance whose embed_texts() method is used for centroid
        computation and is passed through to the scoring stages.

    Returns
    -------
    UserProfile
        Fully populated profile snapshot.
    """
    async with db_pool.acquire() as conn:
        # 1. Topics
        topic_rows = await conn.fetch(
            "SELECT id, name, description, query_terms FROM topics ORDER BY name"
        )
        topics = [
            TopicRef(
                id=r["id"],
                name=r["name"],
                description=r.get("description"),
                query_terms=r.get("query_terms") or [],
            )
            for r in topic_rows
        ]

        # 2. Tracked author identifiers (names + s2_author_ids)
        author_rows = await conn.fetch(
            "SELECT author_name, s2_author_id FROM tracked_authors WHERE enabled = TRUE"
        )
        tracked_author_ids: list[str] = []
        for r in author_rows:
            # Prefer s2_author_id as the canonical ID; fall back to author name
            aid = r.get("s2_author_id")
            if aid:
                tracked_author_ids.append(str(aid))
            else:
                tracked_author_ids.append(r["author_name"])

        # 3. Library centroid: embed abstracts of "engaged" papers
        engaged_rows = await conn.fetch(
            """
            SELECT p.id, p.abstract
            FROM papers p
            JOIN paper_user_state pus ON pus.paper_id = p.id
            WHERE pus.status IN ('starred', 'read', 'reading')
              AND p.abstract IS NOT NULL
              AND p.abstract != ''
            """
        )
        library_centroid: list[float] | None = None
        if engaged_rows:
            abstracts = [r["abstract"] for r in engaged_rows]
            try:
                embeddings = await embedder.embed_texts(abstracts)
                if embeddings:
                    dim = len(embeddings[0])
                    centroid = [0.0] * dim
                    for vec in embeddings:
                        for i, v in enumerate(vec):
                            centroid[i] += v
                    n = len(embeddings)
                    library_centroid = [v / n for v in centroid]
            except Exception:
                logger.warning("load_profile: failed to compute library centroid", exc_info=True)
                library_centroid = None

        # 4. Config: weights, deck_size, stage2_top_k
        config_rows = await conn.fetch(
            """
            SELECT key, value FROM user_config
            WHERE key IN ('pulse.weights', 'pulse.deck_size', 'pulse.stage2_top_k')
            """
        )
        cfg: dict[str, Any] = {r["key"]: r["value"] for r in config_rows}
        # asyncpg JSONB auto-decodes — do NOT call json.loads()
        raw_weights = cfg.get("pulse.weights", _DEFAULT_WEIGHTS)
        weights: dict[str, float] = (
            {k: float(v) for k, v in raw_weights.items()}
            if isinstance(raw_weights, dict)
            else dict(_DEFAULT_WEIGHTS)
        )
        deck_size = int(cfg.get("pulse.deck_size", _DEFAULT_DECK_SIZE))
        stage2_top_k = int(cfg.get("pulse.stage2_top_k", _DEFAULT_STAGE2_TOP_K))

        # 5. Recent rating history (top 10 positive + top 10 negative)
        positive_rows = await conn.fetch(
            """
            SELECT p.title
            FROM pulse_ratings pr
            JOIN papers p ON p.id = pr.paper_id
            WHERE pr.rating IN ('up', 'save', 'open')
            ORDER BY pr.created_at DESC
            LIMIT $1
            """,
            _RATING_HISTORY_LIMIT,
        )
        recent_positive_titles = [r["title"] for r in positive_rows][:_RATING_HISTORY_LIMIT]

        negative_rows = await conn.fetch(
            """
            SELECT p.title
            FROM pulse_ratings pr
            JOIN papers p ON p.id = pr.paper_id
            WHERE pr.rating IN ('down', 'dismiss')
            ORDER BY pr.created_at DESC
            LIMIT $1
            """,
            _RATING_HISTORY_LIMIT,
        )
        recent_negative_titles = [r["title"] for r in negative_rows][:_RATING_HISTORY_LIMIT]

    return UserProfile(
        topics=topics,
        tracked_author_ids=tracked_author_ids,
        library_centroid=library_centroid,
        weights=weights,
        deck_size=deck_size,
        stage2_top_k=stage2_top_k,
        recent_positive_titles=recent_positive_titles,
        recent_negative_titles=recent_negative_titles,
    )
