"""Pulse Phase 2 classifier training and scoring."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import pickle
from datetime import UTC, datetime
from typing import Any

from jarvis_common.jobs import JobContext

_HMAC_DIGEST_LEN = 32  # SHA-256 digest length in bytes


def _hmac_key() -> bytes:
    """Return the HMAC signing key for model blobs (read at call time, not import time).

    Resolution order (audit H14 — public-literal fallback removed; M-07
    extended: derivation fallback also forbidden in production):

    1. ``JARVIS_MODEL_HMAC_KEY`` — dedicated env var, preferred. Use this so
       compromise of the HTTP bearer (``JARVIS_API_KEY``) does not also let an
       attacker forge model blobs.
    2. ``sha256(b"model-signing:" + JARVIS_API_KEY)`` — backward-compatible
       derivation. Domain-separated so the derived key cannot collide with any
       direct use of the bearer. **Only permitted outside production.**

    Raises ``RuntimeError`` if no usable key is configured. In production
    (``ENVIRONMENT=production``), ``JARVIS_MODEL_HMAC_KEY`` is mandatory —
    the derivation fallback is refused so a stolen bearer cannot also forge
    model blobs.
    """
    model_key = os.environ.get("JARVIS_MODEL_HMAC_KEY")
    if model_key:
        return model_key.encode()
    if os.environ.get("ENVIRONMENT", "").lower() == "production":
        raise RuntimeError(
            "JARVIS_MODEL_HMAC_KEY must be set in production (no derivation fallback)"
        )
    api_key = os.environ.get("JARVIS_API_KEY")
    if api_key:
        return hashlib.sha256(b"model-signing:" + api_key.encode()).digest()
    raise RuntimeError(
        "Pulse model HMAC key required: set JARVIS_MODEL_HMAC_KEY (preferred) "
        "or JARVIS_API_KEY. The previous public-literal fallback was removed "
        "by audit H14 — see docs/SECURITY.md#pulse-model-signing."
    )


def _sign_blob(blob: bytes) -> bytes:
    """Prepend a 32-byte SHA-256 HMAC to *blob* so load can verify authenticity."""
    mac = hmac.digest(_hmac_key(), blob, "sha256")
    return mac + blob


def _verify_and_unpickle(signed: bytes) -> Any:
    """Verify the HMAC prefix and unpickle; raises ValueError on tamper or short blob."""
    if len(signed) < _HMAC_DIGEST_LEN:
        raise ValueError("model blob too short to contain HMAC signature")
    mac, data = signed[:_HMAC_DIGEST_LEN], signed[_HMAC_DIGEST_LEN:]
    expected = hmac.digest(_hmac_key(), data, "sha256")
    if not hmac.compare_digest(mac, expected):
        raise ValueError("model blob HMAC mismatch — tampered blob or key rotation required")
    return pickle.loads(data)  # noqa: S301 — HMAC-verified blob


logger = logging.getLogger(__name__)

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


def _has_feature_signals(signals: Any) -> bool:
    return isinstance(signals, dict) and bool(signals)


async def train_classifier_model(
    db_pool: Any,
    *,
    min_ratings: int = MIN_RATINGS,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Train and persist an active logistic classifier when enough ratings exist."""
    try:
        import sklearn
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
        fetched_rows = await conn.fetch(
            """
            SELECT pc.signals AS signals, rf.signal AS rating
            FROM recommendation_feedback rf
            JOIN pulse_cards pc
              ON pc.paper_id = rf.paper_id
             AND pc.user_id IS NOT DISTINCT FROM $1
            WHERE rf.source IN (
                'pulse_thumbs',
                'feed_thumbs',
                'paper_detail_thumbs',
                'dismiss_combined'
            )
              AND rf.user_id IS NOT DISTINCT FROM $1
              AND pc.signals <> '{}'::jsonb
            ORDER BY rf.created_at DESC
            LIMIT 1000
            """,
            user_id,
        )
    rows = [row for row in fetched_rows if _has_feature_signals(row["signals"])]
    excluded_missing_signal_count = len(fetched_rows) - len(rows)

    def _with_excluded_count(result: dict[str, Any]) -> dict[str, Any]:
        if excluded_missing_signal_count:
            result["excluded_missing_signal_count"] = excluded_missing_signal_count
        return result

    if len(rows) < min_ratings:
        return _with_excluded_count(
            {
                "trained": False,
                "available": True,
                "sample_count": len(rows),
                "degradation_reason": f"need at least {min_ratings} ratings",
            }
        )

    labels = [1 if row["rating"] == "positive" else 0 for row in rows]
    if len(set(labels)) < 2:
        return _with_excluded_count(
            {
                "trained": False,
                "available": True,
                "sample_count": len(rows),
                "degradation_reason": "need both positive and negative ratings",
            }
        )

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
    if excluded_missing_signal_count:
        metrics["excluded_missing_signal_count"] = excluded_missing_signal_count
    if val_y and len(set(val_y)) == 2:
        val_probabilities = model.predict_proba(val_x)
        metrics["auc"] = float(roc_auc_score(val_y, [row[1] for row in val_probabilities]))
        metrics["auc_degradation_reason"] = None
    else:
        metrics["auc"] = None
        metrics["auc_degradation_reason"] = "validation split lacks both classes"
    blob = _sign_blob(
        pickle.dumps(
            {
                "model": model,
                "sklearn_version": sklearn.__version__,
                "trained_at": datetime.now(UTC).isoformat(),
            }
        )
    )

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE pulse_models
                   SET is_active = FALSE
                 WHERE is_active = TRUE
                   AND user_id IS NOT DISTINCT FROM $1
                """,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO pulse_models
                    (user_id, model_version, model_blob, feature_names, metrics, is_active)
                VALUES ($1, 'v1', $2, $3::jsonb, $4::jsonb, TRUE)
                """,
                user_id,
                blob,
                FEATURE_NAMES,
                metrics,
            )
    return {"trained": True, "available": True, **metrics}


async def load_active_classifier(
    db_pool: Any,
    *,
    user_id: int | None = None,
) -> tuple[Any | None, dict[str, Any]]:
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
              AND user_id IS NOT DISTINCT FROM $1
            ORDER BY trained_at DESC
            LIMIT 1
            """,
            user_id,
        )
    if row is None:
        return None, {"available": False, "degradation_reason": "no active model"}
    try:
        raw = _verify_and_unpickle(bytes(row["model_blob"]))
        # Support both legacy format (bare model) and new format (dict with metadata)
        if isinstance(raw, dict) and "model" in raw:
            import sklearn

            model = raw["model"]
            saved_version = raw.get("sklearn_version")
            if saved_version and saved_version != sklearn.__version__:
                logger.warning(
                    "load_active_classifier: sklearn version mismatch — "
                    "model trained with %s, current %s; predictions may differ",
                    saved_version,
                    sklearn.__version__,
                )
        else:
            # Legacy: bare model pickle (no version metadata)
            model = raw
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
    *,
    user_id: int | None = None,
) -> tuple[list[float], dict[str, Any]]:
    """Score signal dicts with the active classifier, returning zeros on fallback."""
    model, meta = await load_active_classifier(db_pool, user_id=user_id)
    if model is None:
        return [0.0 for _ in signal_dicts], meta
    probabilities = model.predict_proba([_feature_vector(s) for s in signal_dicts])
    return [float(row[1]) for row in probabilities], meta


async def _pulse_train_classifier_job(
    pool: Any,
    http_client: Any,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Background job handler for Pulse classifier retraining."""
    user_id = payload.get("user_id")
    await ctx.update_progress(0.1, "Training Pulse classifier")
    result = await train_classifier_model(pool, user_id=user_id)
    await ctx.update_progress(1.0, "Done")
    return result
