"""Tests for optional Pulse classifier training."""

from __future__ import annotations

import builtins
import pickle
import sys
from types import ModuleType

import pytest
from paper_ingestion.pulse.training import (
    FEATURE_NAMES,
    classifier_scores,
    load_active_classifier,
    train_classifier_model,
)
from tests.conftest import FakeRecord, _make_pool_and_conn


class FakeLogisticRegression:
    """Pickleable sklearn stand-in used to keep scikit-learn optional in tests."""

    def __init__(self, *, random_state: int | None = None, max_iter: int | None = None):
        self.random_state = random_state
        self.max_iter = max_iter
        self.fitted_rows = 0
        self.labels: list[int] = []

    def fit(self, x: list[list[float]], labels: list[int]) -> FakeLogisticRegression:
        self.fitted_rows = len(x)
        self.labels = list(labels)
        return self

    def score(self, _x: list[list[float]], _labels: list[int]) -> float:
        return 0.875

    def predict_proba(self, x: list[list[float]]) -> list[list[float]]:
        return [[0.7, 0.3] if row[0] < 0.5 else [0.2, 0.8] for row in x]


def _block_sklearn_import(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == "sklearn" or name.startswith("sklearn."):
            raise ImportError("sklearn intentionally unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _install_fake_sklearn(monkeypatch: pytest.MonkeyPatch) -> None:
    sklearn_mod = ModuleType("sklearn")
    sklearn_mod.__path__ = []  # type: ignore[attr-defined]
    linear_model_mod = ModuleType("sklearn.linear_model")
    metrics_mod = ModuleType("sklearn.metrics")
    linear_model_mod.LogisticRegression = FakeLogisticRegression  # type: ignore[attr-defined]
    metrics_mod.roc_auc_score = lambda _labels, _scores: 0.75  # type: ignore[attr-defined]
    sklearn_mod.linear_model = linear_model_mod  # type: ignore[attr-defined]
    sklearn_mod.metrics = metrics_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sklearn", sklearn_mod)
    monkeypatch.setitem(sys.modules, "sklearn.linear_model", linear_model_mod)
    monkeypatch.setitem(sys.modules, "sklearn.metrics", metrics_mod)


def _feedback_rows(count: int, *, source: str = "pulse_thumbs") -> list[FakeRecord]:
    """Return FakeRecord rows shaped like recommendation_feedback JOIN pulse_cards.

    Column ``rating`` is the alias for ``rf.signal`` declared in the SQL
    (``rf.signal AS rating``), so downstream label mapping reads ``row["rating"]``.
    Values are binary: ``'positive'`` / ``'negative'``.
    """
    signals = ["positive", "negative"]
    rows: list[FakeRecord] = []
    for idx in range(count):
        rows.append(
            FakeRecord(
                {
                    "rating": signals[idx % len(signals)],
                    "signals": {
                        "embedding": 0.1 + idx / 100.0,
                        "topic": 0.2,
                        "recency": 0.3,
                        "author_bonus": 0.0,
                        "llm_relevance": 0.4,
                        "llm_novelty": 0.5,
                        "citation_pagerank": 0.6,
                        "citation_count": 0.7,
                        "citation_adamic_adar": 0.8,
                    },
                }
            )
        )
    return rows


# Keep legacy alias so any future callers get feedback rows automatically.
_rating_rows = _feedback_rows


@pytest.mark.asyncio
async def test_train_classifier_missing_sklearn_falls_back(monkeypatch: pytest.MonkeyPatch):
    _block_sklearn_import(monkeypatch)
    pool, _conn = _make_pool_and_conn()

    result = await train_classifier_model(pool)

    assert result == {
        "trained": False,
        "available": False,
        "sample_count": 0,
        "degradation_reason": "scikit-learn not installed",
    }
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_train_classifier_requires_minimum_ratings(monkeypatch: pytest.MonkeyPatch):
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = _rating_rows(3)

    result = await train_classifier_model(pool, min_ratings=4)

    assert result == {
        "trained": False,
        "available": True,
        "sample_count": 3,
        "degradation_reason": "need at least 4 ratings",
    }
    conn.fetch.assert_awaited_once()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_train_classifier_persists_active_fake_model(monkeypatch: pytest.MonkeyPatch):
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = _rating_rows(6)

    result = await train_classifier_model(pool, min_ratings=6)

    # 6 rows with binary signal (positive/negative alternating): 3 positive, 3 negative.
    # Both val-split arms have both classes, so AUC is computed (fake returns 0.75).
    assert result["trained"] is True
    assert result["available"] is True
    assert result["sample_count"] == 6
    assert result["train_accuracy"] == 0.875
    assert conn.execute.await_count == 2
    conn.transaction.assert_called_once()

    deactivate_call, insert_call = conn.execute.await_args_list
    assert "UPDATE pulse_models" in deactivate_call.args[0]
    assert "SET is_active = FALSE" in deactivate_call.args[0]
    assert "user_id IS NOT DISTINCT FROM $1" in deactivate_call.args[0]
    assert "INSERT INTO pulse_models" in insert_call.args[0]
    # args[1] = user_id (new param from Group B user_id threading)
    model_blob = insert_call.args[2]
    stored_feature_names = insert_call.args[3]
    metrics = insert_call.args[4]

    model = pickle.loads(model_blob)
    assert isinstance(model, FakeLogisticRegression)
    # 6 balanced rows → 20% val split takes 1 positive + 1 negative → 4 train rows.
    assert model.fitted_rows == 4
    assert set(model.labels) == {0, 1}
    assert stored_feature_names == FEATURE_NAMES
    assert metrics["sample_count"] == 6
    assert metrics["train_accuracy"] == 0.875


@pytest.mark.asyncio
async def test_train_classifier_records_auc_when_validation_has_both_classes(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = _feedback_rows(40)

    result = await train_classifier_model(pool, min_ratings=40)

    assert result["auc"] == 0.75
    assert result["auc_degradation_reason"] is None
    metrics = conn.execute.await_args_list[1].args[4]
    assert metrics["auc"] == 0.75


# ---------------------------------------------------------------------------
# New tests: recommendation_feedback table integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_train_classifier_empty_recommendation_feedback_returns_no_data_sentinel(
    monkeypatch: pytest.MonkeyPatch,
):
    """Empty recommendation_feedback → trained=False with sample_count=0."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    result = await train_classifier_model(pool, min_ratings=10)

    assert result["trained"] is False
    assert result["available"] is True
    assert result["sample_count"] == 0
    assert "need at least" in result["degradation_reason"]
    conn.fetch.assert_awaited_once()
    # No model write should happen.
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_train_classifier_pulse_thumbs_source_succeeds(
    monkeypatch: pytest.MonkeyPatch,
):
    """Rows sourced from pulse_thumbs are accepted and produce a trained model."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = _feedback_rows(6, source="pulse_thumbs")

    result = await train_classifier_model(pool, min_ratings=6)

    assert result["trained"] is True
    assert result["available"] is True
    assert result["sample_count"] == 6


@pytest.mark.asyncio
async def test_train_classifier_dismiss_combined_source_succeeds(
    monkeypatch: pytest.MonkeyPatch,
):
    """Rows sourced from dismiss_combined are accepted and produce a trained model."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = _feedback_rows(6, source="dismiss_combined")

    result = await train_classifier_model(pool, min_ratings=6)

    assert result["trained"] is True
    assert result["available"] is True
    assert result["sample_count"] == 6


@pytest.mark.asyncio
async def test_train_classifier_sql_references_recommendation_feedback(
    monkeypatch: pytest.MonkeyPatch,
):
    """The SQL passed to conn.fetch must reference recommendation_feedback, not pulse_ratings."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    # Return fewer rows than threshold so we exit early after the query.
    conn.fetch.return_value = []

    await train_classifier_model(pool, min_ratings=1)

    conn.fetch.assert_awaited_once()
    sql: str = conn.fetch.await_args.args[0]
    assert "recommendation_feedback" in sql, (
        "SQL must reference recommendation_feedback (migration 049 dropped pulse_ratings)"
    )
    assert "pulse_ratings" not in sql, "SQL must NOT reference the dropped pulse_ratings table"
    for source in ("pulse_thumbs", "feed_thumbs", "paper_detail_thumbs", "dismiss_combined"):
        assert source in sql


@pytest.mark.asyncio
async def test_train_classifier_sql_includes_all_explicit_feedback_sources(
    monkeypatch: pytest.MonkeyPatch,
):
    """Training consumes explicit thumbs from Pulse, feed, detail, and combined dismiss."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    await train_classifier_model(pool, min_ratings=1)

    sql: str = conn.fetch.await_args.args[0]
    assert "'pulse_thumbs'" in sql
    assert "'feed_thumbs'" in sql
    assert "'paper_detail_thumbs'" in sql
    assert "'dismiss_combined'" in sql
    assert "JOIN pulse_cards" in sql
    assert "COALESCE(pc.signals" not in sql
    assert "pc.signals <> '{}'::jsonb" in sql


@pytest.mark.asyncio
async def test_train_classifier_ignores_feedback_without_feature_signals(
    monkeypatch: pytest.MonkeyPatch,
):
    """Explicit feedback without feature signals must not train zero-vector examples."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    rows: list[FakeRecord] = []
    for idx in range(6):
        rows.append(
            FakeRecord(
                {
                    "rating": "positive" if idx < 3 else "negative",
                    "signals": {},
                }
            )
        )
    conn.fetch.return_value = rows

    result = await train_classifier_model(pool, min_ratings=6)

    assert result == {
        "trained": False,
        "available": True,
        "sample_count": 0,
        "degradation_reason": "need at least 6 ratings",
        "excluded_missing_signal_count": 6,
    }
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_train_classifier_binary_signal_label_mapping(
    monkeypatch: pytest.MonkeyPatch,
):
    """'positive' maps to label 1, 'negative' maps to label 0 — no 5-state logic."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    # 4 positives + 2 negatives → both classes present, model trains.
    positive_rows = _feedback_rows(4, source="pulse_thumbs")
    # Force all to 'positive'.
    for row in positive_rows:
        row["rating"] = "positive"
    negative_rows = _feedback_rows(2, source="dismiss_combined")
    for row in negative_rows:
        row["rating"] = "negative"
    conn.fetch.return_value = positive_rows + negative_rows

    result = await train_classifier_model(pool, min_ratings=6)

    assert result["trained"] is True
    # Verify the stored model blob contains correctly mapped labels.
    insert_call = conn.execute.await_args_list[1]
    # args[1] = user_id (new param from Group B user_id threading)
    model_blob = insert_call.args[2]
    model = pickle.loads(model_blob)
    assert isinstance(model, FakeLogisticRegression)
    assert set(model.labels) == {0, 1}, (
        "Labels must be binary {0, 1} — old 5-state mapping ('up'/'save'/'open') must be gone"
    )


@pytest.mark.asyncio
async def test_load_active_classifier_filters_by_user_id(monkeypatch: pytest.MonkeyPatch):
    """Active classifier loading must select the model for the requested user."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None

    model, meta = await load_active_classifier(pool, user_id=55)

    assert model is None
    assert meta == {"available": False, "degradation_reason": "no active model"}
    sql: str = conn.fetchrow.await_args.args[0]
    params = conn.fetchrow.await_args.args[1:]
    assert "user_id IS NOT DISTINCT FROM $1" in sql
    assert params == (55,)


@pytest.mark.asyncio
async def test_classifier_scores_loads_user_scoped_model(monkeypatch: pytest.MonkeyPatch):
    """Scoring must use the active classifier for the same user scope as training."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    model_blob = pickle.dumps(FakeLogisticRegression())
    conn.fetchrow.return_value = FakeRecord(
        {
            "model_blob": model_blob,
            "feature_names": FEATURE_NAMES,
            "metrics": {"sample_count": 6},
            "trained_at": None,
        }
    )

    scores, meta = await classifier_scores(pool, [{"embedding": 1.0}], user_id=55)

    assert scores == [0.8]
    assert meta["available"] is True
    sql: str = conn.fetchrow.await_args.args[0]
    params = conn.fetchrow.await_args.args[1:]
    assert "user_id IS NOT DISTINCT FROM $1" in sql
    assert params == (55,)


@pytest.mark.asyncio
async def test_train_classifier_deactivates_only_requested_user_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """Training a per-user model must not deactivate active models for other users."""
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = _feedback_rows(6)

    result = await train_classifier_model(pool, min_ratings=6, user_id=55)

    assert result["trained"] is True
    deactivate_call = conn.execute.await_args_list[0]
    assert "user_id IS NOT DISTINCT FROM $1" in deactivate_call.args[0]
    assert deactivate_call.args[1:] == (55,)
