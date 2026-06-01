"""UserProfile model and load_profile() for the Pulse scoring pipeline.

Aggregates all per-user context needed by the three-stage scorer:
topics, tracked authors, library centroid, config weights, and rating history.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from paper_ingestion.models import TopicRef
from paper_ingestion.queries.predicates import VIEW_PREDICATES

logger = logging.getLogger(__name__)

# Sensible defaults mirroring migration 018 seed values
_DEFAULT_DECK_SIZE = 10
_DEFAULT_STAGE2_TOP_K = 40
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
    # Lifecycle redesign fields
    user_id: int | None = None
    negative_topics: list[str] = Field(default_factory=list)
    negative_authors: list[str] = Field(default_factory=list)
    negative_centroid: list[float] | None = None
    dampened_topics: set[int] = Field(default_factory=set)
    # Pulse reliability — discovery lookback window (days), clamped 1-90
    lookback_days: int = Field(default=7, ge=1, le=90)
    # Startup grace before first outbound HTTP burst (seconds)
    startup_grace_seconds: float = Field(default=0.0, ge=0.0, le=300.0)
    # L2 negative-centroid penalty coefficient — validated [0.0, 2.0]; default 0.5.
    # Kept separate from `weights` so stage3_combine does not iterate it as a signal.
    l2_lambda: float = Field(default=0.5, ge=0.0, le=2.0)


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
        rows matching this user (``rf.user_id IS NOT DISTINCT FROM $N``).
        When None (single-user / system mode), no user_id filter is applied and
        all ratings are returned — preserving the existing single-tenant behaviour.

    Returns
    -------
    UserProfile
        Fully populated profile snapshot.

    Notes
    -----
    The function intentionally uses two separate connection acquisitions with
    the HTTP calls to embed_texts() running in between (no connection held), so
    that potentially slow embedding round-trips do not block pool slots
    (BE-001 / PI-006).
    """
    # ------------------------------------------------------------------
    # Fetch centroid-feed data (topics, authors, engaged papers).
    # Release the connection before the HTTP call to the embedder.
    # ------------------------------------------------------------------
    async with db_pool.acquire() as conn:
        # 1. Topics — scoped to the user's subscriptions when user_id is set
        # (migration 074: user_topic_subscriptions is the per-user subscription
        # table; topics itself is a global catalogue with no user_id column).
        # In single-tenant / system mode (user_id=None) all topics are returned
        # to preserve the pre-Sprint-A behaviour.
        if user_id is not None:
            topic_rows = await conn.fetch(
                """SELECT t.id, t.name, t.description, t.query_terms
                   FROM topics t
                   JOIN user_topic_subscriptions uts ON uts.topic_id = t.id
                   WHERE uts.user_id = $1
                   ORDER BY t.name""",
                user_id,
            )
        else:
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

        # 2. Tracked author identifiers — scoped to the user when user_id is set
        # (migration 070 added user_id to tracked_authors).  NULL rows are
        # system-shared and included in single-tenant mode (user_id=None).
        author_rows = await conn.fetch(
            """SELECT author_name, s2_author_id
               FROM tracked_authors
               WHERE enabled = TRUE
                 AND user_id IS NOT DISTINCT FROM $1""",
            user_id,
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

        # 3. Collect abstracts of "engaged" papers for library centroid computation.
        # state column replaces the old status/saved columns; starred is orthogonal.
        engaged_rows = await conn.fetch(
            f"""
            SELECT p.id, p.abstract
            FROM papers p
            JOIN paper_user_state pus ON pus.paper_id = p.id
            WHERE ($1::int IS NULL OR pus.user_id IS NOT DISTINCT FROM $1)
              AND (
                COALESCE(pus.starred, FALSE)
                OR {VIEW_PREDICATES["library"]}
            )
              AND p.abstract IS NOT NULL
              AND p.abstract != ''
            """,
            user_id,
        )
        abstracts = [r["abstract"] for r in engaged_rows]
    # Connection released — centroid-feed fetch complete.

    # ------------------------------------------------------------------
    # HTTP call for library centroid (no DB connection held, PI-006)
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
    # Fetch config + rating history + L1/L2/L3 signals
    # ------------------------------------------------------------------
    async with db_pool.acquire() as conn:
        # 4. Config: weights, deck_size, stage2_top_k, l2_lambda
        # Prefer per-user row; fall back to NULL-row global default.
        config_rows = await conn.fetch(
            """
            SELECT key, value, user_id FROM user_config
            WHERE key IN (
                'pulse.weights', 'pulse.deck_size', 'pulse.stage2_top_k', 'pulse.l2_lambda',
                'pulse.lookback_days', 'pulse.startup_grace_seconds'
            )
            AND (user_id IS NOT DISTINCT FROM $1 OR user_id IS NULL)
            ORDER BY key, user_id NULLS LAST
            """,
            user_id,
        )
        # Build cfg dict: for each key, the user-specific row (non-NULL user_id) wins
        # over the global default (NULL user_id). Rows are ordered per-key with
        # user-specific rows first (NULLS LAST on user_id).
        _cfg_raw: dict[str, Any] = {}
        for r in config_rows:
            k = r["key"]
            # asyncpg Record supports .get(); prefer user-specific row (non-NULL user_id)
            # over global default (NULL user_id).
            if k not in _cfg_raw or r.get("user_id") is not None:
                _cfg_raw[k] = r["value"]
        cfg: dict[str, Any] = _cfg_raw
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

        # Read l2_lambda — validated by A2 config validator ([0.0, 2.0]); default 0.5.
        # Stored as a dedicated UserProfile field, NOT inside weights, so that
        # stage3_combine never iterates it as a scoring signal (DOM-B-01).
        raw_l2 = cfg.get("pulse.l2_lambda")
        l2_lambda: float = float(raw_l2) if raw_l2 is not None else 0.5

        raw_lookback = cfg.get("pulse.lookback_days")
        lookback_days: int = max(1, min(90, int(raw_lookback))) if raw_lookback is not None else 7
        raw_grace = cfg.get("pulse.startup_grace_seconds")
        startup_grace_seconds: float = (
            max(0.0, min(300.0, float(raw_grace))) if raw_grace is not None else 0.0
        )

        # 5. Recent positive ratings (recommendation_feedback, 90-day window).
        # GROUP BY + MAX(created_at) gives "N most recent distinct papers";
        # SELECT DISTINCT + ORDER BY rf.created_at is invalid (Postgres
        # rejects: ORDER BY columns must appear in SELECT list under DISTINCT).
        positive_rows = await conn.fetch(
            """
            SELECT p.id, p.title
              FROM recommendation_feedback rf
              JOIN papers p ON p.id = rf.paper_id
             WHERE rf.signal = 'positive'
               AND rf.created_at > NOW() - INTERVAL '90 days'
               AND rf.user_id IS NOT DISTINCT FROM $1
             GROUP BY p.id, p.title
             ORDER BY MAX(rf.created_at) DESC
             LIMIT $2
            """,
            user_id,
            _RATING_HISTORY_LIMIT,
        )

        # 6. Recent negative ratings — titles only.
        negative_rows = await conn.fetch(
            """
            SELECT p.title
              FROM recommendation_feedback rf
              JOIN papers p ON p.id = rf.paper_id
             WHERE rf.signal = 'negative'
               AND rf.created_at > NOW() - INTERVAL '90 days'
               AND rf.user_id IS NOT DISTINCT FROM $1
             GROUP BY p.title
             ORDER BY MAX(rf.created_at) DESC
             LIMIT $2
            """,
            user_id,
            _RATING_HISTORY_LIMIT,
        )

        recent_positive_titles = [r["title"] for r in positive_rows][:_RATING_HISTORY_LIMIT]
        liked_paper_ids = [r.get("id") for r in positive_rows if r.get("id") is not None]
        recent_negative_titles = [r["title"] for r in negative_rows][:_RATING_HISTORY_LIMIT]

        # 7. L1 — negative topics (top 10 by negative-feedback count, 90-day window).
        neg_topic_rows = await conn.fetch(
            """
            SELECT t.name, COUNT(*) AS neg_count
              FROM recommendation_feedback rf
              JOIN papers p ON p.id = rf.paper_id
              JOIN paper_topics pt ON pt.paper_id = p.id
              JOIN topics t ON t.id = pt.topic_id
             WHERE rf.signal = 'negative'
               AND rf.created_at > NOW() - INTERVAL '90 days'
               AND rf.user_id IS NOT DISTINCT FROM $1
             GROUP BY t.name
             ORDER BY neg_count DESC
             LIMIT 10
            """,
            user_id,
        )
        negative_topics: list[str] = [r["name"] for r in neg_topic_rows]

        # 8. L1 — negative authors (top 10 by negative-feedback count, 90-day window).
        neg_author_rows = await conn.fetch(
            """
            SELECT author, COUNT(*) AS neg_count
              FROM (
                SELECT UNNEST(p.authors) AS author
                  FROM recommendation_feedback rf
                  JOIN papers p ON p.id = rf.paper_id
                 WHERE rf.signal = 'negative'
                   AND rf.created_at > NOW() - INTERVAL '90 days'
                   AND rf.user_id IS NOT DISTINCT FROM $1
              ) authors_expanded
             GROUP BY author
             ORDER BY neg_count DESC
             LIMIT 10
            """,
            user_id,
        )
        negative_authors: list[str] = [r["author"] for r in neg_author_rows]

        # 9. L3 — dampened topics (≥5 negatives in 90d window), per spec §7.3.2.
        dampened_rows = await conn.fetch(
            """
            SELECT t.id, COUNT(*) AS neg_count
              FROM recommendation_feedback rf
              JOIN papers p ON p.id = rf.paper_id
              JOIN paper_topics pt ON pt.paper_id = p.id
              JOIN topics t ON t.id = pt.topic_id
             WHERE rf.signal = 'negative'
               AND rf.created_at > NOW() - INTERVAL '90 days'
               AND rf.user_id IS NOT DISTINCT FROM $1
             GROUP BY t.id
            HAVING COUNT(*) >= 5
            """,
            user_id,
        )
        # rows ordered by neg_count desc for deterministic cap truncation
        dampened_rows_sorted = sorted(dampened_rows, key=lambda r: r["neg_count"], reverse=True)

        # 10. L3 dampening cap (spec §7.3.4): dampened_topics must not exceed
        # 50% of all topic slots — prevents over-dampening a sparse topic list.
        topic_count = len(topics)
        dampened_set: set[int] = set()
        if dampened_rows_sorted:
            cap = max(0, topic_count // 2)  # floor(0.5 × topic_count)
            if len(dampened_rows_sorted) > cap:
                logger.warning(
                    "load_profile: dampened_topics (%d) exceeds 50%% of topic count (%d); "
                    "truncating to %d",
                    len(dampened_rows_sorted),
                    topic_count,
                    cap,
                )
                dampened_rows_sorted = dampened_rows_sorted[:cap]
            dampened_set = {r["id"] for r in dampened_rows_sorted}

        # 11. L2 — negative abstracts for centroid pre-compute (with 30d half-weight decay).
        neg_abstract_rows = await conn.fetch(
            """
            SELECT p.id, p.abstract,
                   CASE WHEN rf.created_at > NOW() - INTERVAL '30 days'
                        THEN 1.0 ELSE 0.5 END AS weight
              FROM recommendation_feedback rf
              JOIN papers p ON p.id = rf.paper_id
             WHERE rf.signal = 'negative'
               AND rf.created_at > NOW() - INTERVAL '90 days'
               AND rf.user_id IS NOT DISTINCT FROM $1
               AND p.abstract IS NOT NULL
               AND length(p.abstract) > 0
             ORDER BY rf.created_at DESC
             LIMIT 100
            """,
            user_id,
        )
    # Connection released — config and signal fetch complete.

    # ------------------------------------------------------------------
    # Compute L2 negative centroid (no DB connection held)
    # ------------------------------------------------------------------
    negative_centroid: list[float] | None = None
    if neg_abstract_rows:
        try:
            neg_abstracts = [r["abstract"] for r in neg_abstract_rows]
            neg_weights = [float(r["weight"]) for r in neg_abstract_rows]
            neg_embeddings = await embedder.embed_texts(neg_abstracts)
            if neg_embeddings:
                expected_dim = len(neg_embeddings[0])
                weighted_centroid = [0.0] * expected_dim
                total_weight = 0.0
                for vec, w in zip(neg_embeddings, neg_weights):
                    if len(vec) != expected_dim:
                        logger.warning(
                            "load_profile: skipping negative embedding with dim %d != expected %d",
                            len(vec),
                            expected_dim,
                        )
                        continue
                    for i, v in enumerate(vec):
                        weighted_centroid[i] += w * v
                    total_weight += w
                if total_weight > 0.0:
                    negative_centroid = [v / total_weight for v in weighted_centroid]
        except RuntimeError:
            logger.warning("load_profile: failed to compute negative centroid", exc_info=True)
            negative_centroid = None

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
        user_id=user_id,
        negative_topics=negative_topics,
        negative_authors=negative_authors,
        negative_centroid=negative_centroid,
        dampened_topics=dampened_set,
        lookback_days=lookback_days,
        startup_grace_seconds=startup_grace_seconds,
        l2_lambda=l2_lambda,
    )
