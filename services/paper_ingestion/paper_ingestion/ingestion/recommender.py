"""
Recommendation engine — liked centroid + project context.
Called by scheduler (nightly) or via POST /api/recommendations/refresh.
"""

import logging
from typing import Any

import asyncpg

from paper_ingestion.queries.predicates import EXCLUDED_STATE_SQL

_logger = logging.getLogger(__name__)

_DEFAULT_LIKED_WEIGHT = 0.6
_DEFAULT_PROJECT_WEIGHT = 0.4
_MIN_SCORE = 0.25
_MAX_RECOMMENDATIONS = 50


async def refresh_recommendations(app: Any, user_id: int | None = None) -> int:
    """Compute and upsert recommendations. Returns total count saved.

    Parameters
    ----------
    user_id:
        When provided, recommendations are computed for that single user.
        When ``None`` (the nightly-cron path), iterate every non-deleted
        user and run the per-user logic once each — no more NULL-owned
        recommendation writes that leaked across tenants.
    """
    if user_id is not None:
        return await _refresh_recommendations_for_user(app, user_id)

    db_pool = app.state.db_pool
    async with db_pool.acquire() as conn:
        user_ids = [
            r["id"] for r in await conn.fetch("SELECT id FROM users WHERE deleted_at IS NULL")
        ]
    total = 0
    for uid in user_ids:
        try:
            total += await _refresh_recommendations_for_user(app, uid)
        except Exception:
            _logger.exception("recommendation refresh failed for user %d", uid)
    return total


async def _refresh_recommendations_for_user(app: Any, user_id: int) -> int:
    """Compute and upsert recommendations for a single user. Returns count saved."""
    assert user_id is not None, "per-user recommendation refresh requires a real user_id"
    db_pool = app.state.db_pool
    embedder = app.state.embedder

    async with db_pool.acquire() as conn:
        liked_weight, project_weight, enabled = await _read_weights(conn, user_id)

    if not enabled:
        _logger.info("recommendation: disabled via config, skipping")
        return 0

    # --- Pre-read: fetch IDs and project names without holding the conn
    #     across slow HTTP/Qdrant calls. ---
    async with db_pool.acquire() as conn:
        starred_ids = await _get_starred_ids(conn, user_id)
        projects_raw = await conn.fetch(
            "SELECT name, description FROM projects WHERE status = 'active' AND user_id = $1",
            user_id,
        )

    # --- HTTP / Qdrant calls (no DB connection held) ---
    liked_scores: dict[int, tuple[float, str]] = {}
    if starred_ids:
        results = await embedder.discover_from_seeds(
            starred_ids, db_pool, limit=_MAX_RECOMMENDATIONS, score_threshold=0.3, user_id=user_id
        )
        for paper_id, score in _aggregate_to_papers(results):
            liked_scores[paper_id] = (score, f"similar to {len(starred_ids)} starred paper(s)")

    project_scores: dict[int, tuple[float, str]] = {}
    for proj in projects_raw:
        text = f"{proj['name']}. {proj['description'] or ''}".strip()
        if not text:
            continue
        results = await embedder.search_similar(
            text, limit=20, score_threshold=0.3, user_id=user_id
        )
        for paper_id, score in _aggregate_to_papers(results):
            existing = project_scores.get(paper_id, (0.0, ""))
            if score > existing[0]:
                project_scores[paper_id] = (score, f"relevant to project '{proj['name']}'")

    if not liked_scores and not project_scores:
        _logger.info("recommendation: no signals available (no starred papers or active projects)")
        return 0

    # Merge signals
    all_paper_ids = set(liked_scores) | set(project_scores)
    merged: list[dict] = []
    for pid in all_paper_ids:
        liked_s, liked_r = liked_scores.get(pid, (0.0, ""))
        proj_s, proj_r = project_scores.get(pid, (0.0, ""))
        score = _compute_score(liked_s, proj_s, liked_weight, project_weight)
        if score < _MIN_SCORE:
            continue
        modes = []
        reasons = []
        if liked_s > 0:
            modes.append("liked")
            reasons.append(liked_r)
        if proj_s > 0:
            modes.append("project")
            reasons.append(proj_r)
        merged.append(
            {"paper_id": pid, "score": score, "modes": modes, "explanation": "; ".join(reasons)}
        )

    if not merged:
        return 0

    # --- Post-write: persist results with a fresh connection ---
    async with db_pool.acquire() as conn:
        unread_ids = await _filter_unread(conn, [r["paper_id"] for r in merged], user_id)
        merged = [r for r in merged if r["paper_id"] in unread_ids]

        if not merged:
            return 0

        # Migration 063 dropped the single-column unique on paper_id and replaced
        # it with UNIQUE NULLS NOT DISTINCT (paper_id, user_id). The ON CONFLICT
        # target must reference that composite constraint, AND user_id must be
        # included in the INSERT — otherwise the upsert errors with
        # "no unique or exclusion constraint matching the ON CONFLICT
        # specification" the moment a duplicate row exists.
        upsert_sql = (
            "INSERT INTO paper_recommendations"
            " (paper_id, user_id, score, modes, explanation, recommended_at)"
            " VALUES ($1, $2, $3, $4, $5, NOW())"
            " ON CONFLICT (paper_id, user_id) DO UPDATE"
            " SET score = EXCLUDED.score, modes = EXCLUDED.modes,"
            "     explanation = EXCLUDED.explanation, recommended_at = NOW(),"
            "     dismissed = FALSE"
        )
        await conn.executemany(
            upsert_sql,
            [(r["paper_id"], user_id, r["score"], r["modes"], r["explanation"]) for r in merged],
        )
    _logger.info("recommendation: saved %d recommendations", len(merged))
    return len(merged)


def _safe_float(val: object, default: float) -> float:
    try:
        return float(val)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default


async def _read_weights(conn: asyncpg.Connection, user_id: int) -> tuple[float, float, bool]:
    """Return (liked_weight, project_weight, enabled) for *user_id*.

    User-row-wins: if a per-user row exists for the caller's user_id it takes
    precedence over the global (user_id IS NULL) row, mirroring the idiom used
    in ``load_profile`` for pulse config.
    """
    rows = await conn.fetch(
        "SELECT key, value, user_id FROM user_config"
        " WHERE key IN"
        " ('recommendation.liked_weight',"
        " 'recommendation.project_weight',"
        " 'recommendation.enabled')"
        " AND (user_id IS NOT DISTINCT FROM $1 OR user_id IS NULL)"
        " ORDER BY key, user_id NULLS LAST",
        user_id,
    )
    # Per-key: user-specific row (non-NULL user_id) wins over global NULL row.
    # Rows are ordered per-key with user-specific rows first (NULLS LAST on user_id).
    _cfg_raw: dict[str, object] = {}
    for r in rows:
        k = r["key"]
        if k not in _cfg_raw or r.get("user_id") is not None:
            _cfg_raw[k] = r["value"]
    liked = _safe_float(
        _cfg_raw.get("recommendation.liked_weight", _DEFAULT_LIKED_WEIGHT),
        _DEFAULT_LIKED_WEIGHT,
    )
    project = _safe_float(
        _cfg_raw.get("recommendation.project_weight", _DEFAULT_PROJECT_WEIGHT),
        _DEFAULT_PROJECT_WEIGHT,
    )
    enabled_val = _cfg_raw.get("recommendation.enabled", True)
    enabled = bool(enabled_val) if not isinstance(enabled_val, bool) else enabled_val
    return liked, project, enabled


async def _get_starred_ids(conn: asyncpg.Connection, user_id: int) -> list[int]:
    rows = await conn.fetch(
        "SELECT paper_id FROM paper_user_state WHERE COALESCE(starred, FALSE)   AND user_id = $1",
        user_id,
    )
    return [r["paper_id"] for r in rows]


def _compute_score(
    liked: float, project: float, liked_weight: float, project_weight: float
) -> float:
    """Return the weighted recommendation score for a candidate paper.

    Parameters
    ----------
    liked:
        Similarity score from the liked-centroid signal (0.0–1.0).
    project:
        Similarity score from the project-context signal (0.0–1.0).
    liked_weight:
        Weight applied to *liked* (default ``_DEFAULT_LIKED_WEIGHT = 0.6``).
    project_weight:
        Weight applied to *project* (default ``_DEFAULT_PROJECT_WEIGHT = 0.4``).
    """
    return liked * liked_weight + project * project_weight


async def _filter_unread(conn: asyncpg.Connection, paper_ids: list[int], user_id: int) -> set[int]:
    # Hard-exclude papers whose lifecycle state is 'trash' or 'done',
    # and papers with a negative recommendation_feedback signal in the last 60 days.
    # Papers with no user_state row (COALESCE to 'inbox') remain eligible.
    if not paper_ids:
        return set()
    rows = await conn.fetch(
        f"""SELECT id FROM papers p
               WHERE p.id = ANY($1)
                 AND NOT EXISTS (
                   SELECT 1 FROM paper_user_state pus
                    WHERE pus.paper_id = p.id
                      AND pus.user_id = $2
                      AND {EXCLUDED_STATE_SQL}
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM recommendation_feedback rf
                    WHERE rf.paper_id = p.id
                      AND rf.signal = 'negative'
                      AND rf.created_at > NOW() - INTERVAL '60 days'
                      AND rf.user_id = $2
                 )""",
        paper_ids,
        user_id,
    )
    return {r["id"] for r in rows}


def _aggregate_to_papers(results: list[dict]) -> list[tuple[int, float]]:
    """Aggregate chunk-level Qdrant results to paper level (max score per paper).

    Both discover_from_seeds and search_similar return list[dict] with
    keys ``paper_id`` and ``score``.
    """
    by_paper: dict[int, float] = {}
    for item in results:
        pid = item.get("paper_id")
        score = item.get("score", 0.0)
        if pid is not None and score > by_paper.get(pid, 0.0):
            by_paper[pid] = score
    return list(by_paper.items())
