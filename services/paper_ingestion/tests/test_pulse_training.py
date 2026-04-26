"""Tests for optional Pulse classifier training."""

from __future__ import annotations

import builtins
import pickle
import sys
from types import ModuleType

import pytest
from paper_ingestion.pulse.training import FEATURE_NAMES, train_classifier_model
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


def _rating_rows(count: int) -> list[FakeRecord]:
    ratings = ["up", "save", "open", "down"]
    rows: list[FakeRecord] = []
    for idx in range(count):
        rows.append(
            FakeRecord(
                {
                    "rating": ratings[idx % len(ratings)],
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

    assert result == {
        "trained": True,
        "available": True,
        "sample_count": 6,
        "train_accuracy": 0.875,
        "auc": None,
        "auc_degradation_reason": "validation split lacks both classes",
    }
    assert conn.execute.await_count == 2
    conn.transaction.assert_called_once()

    deactivate_call, insert_call = conn.execute.await_args_list
    assert "UPDATE pulse_models SET is_active = FALSE" in deactivate_call.args[0]
    assert "INSERT INTO pulse_models" in insert_call.args[0]
    model_blob = insert_call.args[1]
    stored_feature_names = insert_call.args[2]
    metrics = insert_call.args[3]

    model = pickle.loads(model_blob)
    assert isinstance(model, FakeLogisticRegression)
    assert model.fitted_rows == 6
    assert set(model.labels) == {0, 1}
    assert stored_feature_names == FEATURE_NAMES
    assert metrics == {
        "sample_count": 6,
        "train_accuracy": 0.875,
        "auc": None,
        "auc_degradation_reason": "validation split lacks both classes",
    }


@pytest.mark.asyncio
async def test_train_classifier_records_auc_when_validation_has_both_classes(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_sklearn(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = _rating_rows(40)

    result = await train_classifier_model(pool, min_ratings=40)

    assert result["auc"] == 0.75
    assert result["auc_degradation_reason"] is None
    metrics = conn.execute.await_args_list[1].args[3]
    assert metrics["auc"] == 0.75
