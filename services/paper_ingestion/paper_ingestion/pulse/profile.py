"""UserProfile model and load_profile() for the Pulse scoring pipeline.

Aggregates all per-user context needed by the three-stage scorer:
topics, tracked authors, library centroid, config weights, and rating history.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from paper_ingestion.models import TopicRef

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
    "citation_pagerank": 0.0,
    "citation_count": 0.0,
    "citation_adamic_adar": 0.0,
    "classifier": 0.0,
}
_RATING_HISTORY_LIMIT = 10


class UserProfile(BaseModel):
    """Snapshot of user context consumed by the Pulse scoring pipeline."""

    topics: list[TopicRef]
    tracked_author_names: set[str]  # display names, lowercased for case-insensitive match
    tracked_author_s2_ids: set[str]  # opaque S2 numeric IDs (e.g. "1730375")
    library_centroid: list[float] | None
    weights: dict[str, float]
    deck_size: int
    stage2_top_k: int
    recent_positive_titles: list[str]
    recent_negative_titles: list[str]
    liked_paper_ids: list[int] = Field(default_factory=list)


async def load_profile(db_pool: Any, *, embedder: Any, user_id: int | None = None) -> UserProfile:
    """Load all user context from the database and compute the library centroid.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.
    embedder:
        Embedder instance whose embed_texts() method is used for centroid
        computation and is passed through to the scoring stages.
    user_id:
        Optional caller user ID.  When provided, rating history is filtered to
        rows matching this user (``pr.user_id IS NOT DISTINCT FROM $N``).
        When None (single-user / system mode), no user_id filter is applied and
        all ratings are returned — preserving the existing single-tenant behaviour.

    Returns
    -------
    UserProfile
        Fully populated profile snapshot.

    Notes
    -----
    The function intentionally uses two separate connection acquisitions with
    the HTTP call to embed_texts() running in between (no connection held), so
    that a potentially slow embedding round-trip does not block a pool slot
    (BE-001 / PI-006).
    """
    # ------------------------------------------------------------------
    # Phase 1 — fetch centroid-feed data (topics, authors, engaged papers)
    # Release the connection before the HTTP call to the embedder.
    # ------------------------------------------------------------------
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

        # 2. Tracked author identifiers — split into names (lowercased) and S2 IDs
        author_rows = await conn.fetch(
            "SELECT author_name, s2_author_id FROM tracked_authors WHERE enabled = TRUE"
        )
        tracked_author_names: set[str] = set()
        tracked_author_s2_ids: set[str] = set()
        for r in author_rows:
            aid = r.get("s2_author_id")
            if aid:
                tracked_author_s2_ids.add(str(aid))
            name = r.get("author_name")
            if name:
                tracked_author_names.add(str(name).lower())

        # 3. Collect abstracts of "engaged" papers for centroid computation
        engaged_rows = await conn.fetch(
            """
            SELECT p.id, p.abstract
            FROM papers p
            JOIN paper_user_state pus ON pus.paper_id = p.id
            WHERE ($1::int IS NULL OR pus.user_id IS NOT DISTINCT FROM $1)
              AND (
                COALESCE(pus.starred, FALSE)
                OR pus.status IN ('starred', 'read', 'reading')
            )
              AND p.abstract IS NOT NULL
              AND p.abstract != ''
            """,
            user_id,
        )
        abstracts = [r["abstract"] for r in engaged_rows]
    # Connection released — Phase 1 complete.

    # ------------------------------------------------------------------
    # Phase 2 — HTTP call to embedder (no DB connection held, see PI-006)
    # ------------------------------------------------------------------
    library_centroid: list[float] | None = None
    if abstracts:
        try:
            embeddings = await embedder.embed_texts(abstracts)
            if embeddings:
                expected_dim = len(embeddings[0])
                centroid = [0.0] * expected_dim
                n = 0
                for vec in embeddings:
                    if len(vec) != expected_dim:
                        logger.warning(
                            "load_profile: skipping embedding with dim %d != expected %d",
                            len(vec),
                            expected_dim,
                        )
                        continue
                    for i, v in enumerate(vec):
                        centroid[i] += v
                    n += 1
                library_centroid = [v / n for v in centroid] if n > 0 else None
        except RuntimeError:
            logger.warning("load_profile: failed to compute library centroid", exc_info=True)
            library_centroid = None

    # ------------------------------------------------------------------
    # Phase 3 — fetch config + rating history, assemble UserProfile
    # ------------------------------------------------------------------
    async with db_pool.acquire() as conn:
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
        weights: dict[str, float] = dict(_DEFAULT_WEIGHTS)
        if isinstance(raw_weights, dict):
            weights.update({k: float(v) for k, v in raw_weights.items()})
        # M11: clamp weights to [0, 1]; negative weights invert ranking, weights >1 swamp scoring.
        out_of_range = any(w < 0.0 or w > 1.0 for w in weights.values())
        weights = {k: min(1.0, max(0.0, w)) for k, w in weights.items()}
        if out_of_range:
            logger.warning("pulse profile weights had values outside [0, 1]; clamped")
        deck_size = int(cfg.get("pulse.deck_size", _DEFAULT_DECK_SIZE))
        stage2_top_k = int(cfg.get("pulse.stage2_top_k", _DEFAULT_STAGE2_TOP_K))

        # 5. Recent rating history (top 10 positive + top 10 negative).
        # Sprint 7 B13: single query path — `$2::int IS NULL` short-circuits
        # the user filter when running in stub/system mode (user_id=None).
        # When a real user_id is bound, IS NOT DISTINCT FROM matches both the
        # caller's id and any NULL-stamped legacy rows (M1 residual).
        positive_rows = await conn.fetch(
            """
            SELECT p.id, p.title
            FROM pulse_ratings pr
            JOIN papers p ON p.id = pr.paper_id
            WHERE pr.rating IN ('up', 'save', 'open')
              AND ($2::int IS NULL OR pr.user_id IS NOT DISTINCT FROM $2)
            ORDER BY pr.created_at DESC
            LIMIT $1
            """,
            _RATING_HISTORY_LIMIT,
            user_id,
        )
        negative_rows = await conn.fetch(
            """
            SELECT p.title
            FROM pulse_ratings pr
            JOIN papers p ON p.id = pr.paper_id
            WHERE pr.rating IN ('down', 'dismiss')
              AND ($2::int IS NULL OR pr.user_id IS NOT DISTINCT FROM $2)
            ORDER BY pr.created_at DESC
            LIMIT $1
            """,
            _RATING_HISTORY_LIMIT,
            user_id,
        )
        recent_positive_titles = [r["title"] for r in positive_rows][:_RATING_HISTORY_LIMIT]
        liked_paper_ids = [r.get("id") for r in positive_rows if r.get("id") is not None]

        recent_negative_titles = [r["title"] for r in negative_rows][:_RATING_HISTORY_LIMIT]

    return UserProfile(
        topics=topics,
        tracked_author_names=tracked_author_names,
        tracked_author_s2_ids=tracked_author_s2_ids,
        library_centroid=library_centroid,
        weights=weights,
        deck_size=deck_size,
        stage2_top_k=stage2_top_k,
        recent_positive_titles=recent_positive_titles,
        recent_negative_titles=recent_negative_titles,
        liked_paper_ids=liked_paper_ids,
    )
