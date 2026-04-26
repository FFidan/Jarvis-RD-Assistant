"""Pulse Phase 2 classifier training and scoring."""

from __future__ import annotations

import pickle
from typing import Any

from jarvis_common.jobs import JobContext, job_handler

FEATURE_NAMES: list[str] = [
    "embedding",
    "topic",
    "recency",
    "author_bonus",
    "llm_relevance",
    "llm_novelty",
    "citation_pagerank",
    "citation_count",
    "citation_adamic_adar",
]
MIN_RATINGS = 30


def _feature_vector(signals: dict[str, Any]) -> list[float]:
    return [float(signals.get(name, 0.0) or 0.0) for name in FEATURE_NAMES]


async def train_classifier_model(db_pool: Any, *, min_ratings: int = MIN_RATINGS) -> dict[str, Any]:
    """Train and persist an active logistic classifier when enough ratings exist."""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
    except ImportError:
        return {
            "trained": False,
            "available": False,
            "sample_count": 0,
            "degradation_reason": "scikit-learn not installed",
        }

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pc.signals, pr.rating
            FROM pulse_ratings pr
            JOIN pulse_cards pc ON pc.paper_id = pr.paper_id
            ORDER BY pr.created_at DESC
            LIMIT 1000
            """
        )
    if len(rows) < min_ratings:
        return {
            "trained": False,
            "available": True,
            "sample_count": len(rows),
            "degradation_reason": f"need at least {min_ratings} ratings",
        }

    labels = [1 if row["rating"] in {"up", "save", "open"} else 0 for row in rows]
    if len(set(labels)) < 2:
        return {
            "trained": False,
            "available": True,
            "sample_count": len(rows),
            "degradation_reason": "need both positive and negative ratings",
        }

    x = [_feature_vector(row["signals"] or {}) for row in rows]
    positives = [idx for idx, label in enumerate(labels) if label == 1]
    negatives = [idx for idx, label in enumerate(labels) if label == 0]
    val_indices: set[int] = set()
    if len(positives) >= 2 and len(negatives) >= 2:
        val_indices.update(positives[-max(1, round(len(positives) * 0.2)) :])
        val_indices.update(negatives[-max(1, round(len(negatives) * 0.2)) :])
    train_indices = [idx for idx in range(len(rows)) if idx not in val_indices]
    val_indices_sorted = sorted(val_indices)
    train_x = [x[idx] for idx in train_indices]
    train_y = [labels[idx] for idx in train_indices]
    val_x = [x[idx] for idx in val_indices_sorted]
    val_y = [labels[idx] for idx in val_indices_sorted]

    model = LogisticRegression(random_state=0, max_iter=500)
    model.fit(train_x, train_y)
    accuracy = float(model.score(train_x, train_y))
    metrics: dict[str, Any] = {"sample_count": len(rows), "train_accuracy": accuracy}
    if val_y and len(set(val_y)) == 2:
        val_probabilities = model.predict_proba(val_x)
        metrics["auc"] = float(roc_auc_score(val_y, [row[1] for row in val_probabilities]))
        metrics["auc_degradation_reason"] = None
    else:
        metrics["auc"] = None
        metrics["auc_degradation_reason"] = "validation split lacks both classes"
    blob = pickle.dumps(model)

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE pulse_models SET is_active = FALSE WHERE is_active = TRUE")
            await conn.execute(
                """
                INSERT INTO pulse_models
                    (user_id, model_version, model_blob, feature_names, metrics, is_active)
                VALUES (NULL, 'v1', $1, $2::jsonb, $3::jsonb, TRUE)
                """,
                blob,
                FEATURE_NAMES,
                metrics,
            )
    return {"trained": True, "available": True, **metrics}


async def load_active_classifier(db_pool: Any) -> tuple[Any | None, dict[str, Any]]:
    """Load the active classifier and metadata, if present and dependencies exist."""
    try:
        import sklearn  # noqa: F401
    except ImportError:
        return None, {"available": False, "degradation_reason": "scikit-learn not installed"}

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT model_blob, feature_names, metrics, trained_at
            FROM pulse_models
            WHERE is_active = TRUE
            ORDER BY trained_at DESC
            LIMIT 1
            """
        )
    if row is None:
        return None, {"available": False, "degradation_reason": "no active model"}
    try:
        model = pickle.loads(bytes(row["model_blob"]))
    except Exception:
        return None, {"available": False, "degradation_reason": "active model could not be loaded"}
    return model, {
        "available": True,
        "feature_names": row["feature_names"] or FEATURE_NAMES,
        "metrics": row["metrics"] or {},
        "trained_at": row["trained_at"].isoformat() if row["trained_at"] else None,
    }


async def classifier_scores(
    db_pool: Any,
    signal_dicts: list[dict[str, Any]],
) -> tuple[list[float], dict[str, Any]]:
    """Score signal dicts with the active classifier, returning zeros on fallback."""
    model, meta = await load_active_classifier(db_pool)
    if model is None:
        return [0.0 for _ in signal_dicts], meta
    probabilities = model.predict_proba([_feature_vector(s) for s in signal_dicts])
    return [float(row[1]) for row in probabilities], meta


@job_handler("pulse.train_classifier")
async def _pulse_train_classifier_job(
    pool: Any,
    http_client: Any,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Background job handler for Pulse classifier retraining."""
    await ctx.update_progress(0.1, "Training Pulse classifier")
    result = await train_classifier_model(pool)
    await ctx.update_progress(1.0, "Done")
    return result
