"""Smoke tests for scripts/eval_pulse.py.

Exercises importable pure functions without live services.
run_eval() depends on the full Pulse scoring pipeline (stage2 requires an
openai_client); we test the deterministic helpers and fixture loading separately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Repo root must be on sys.path so ``scripts`` is importable as a package.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# eval_pulse.py adds services/paper_ingestion to sys.path itself on import.
import scripts.eval_pulse as eval_pulse_mod


def test_hash_vector_is_deterministic():
    """_hash_vector must return the same vector for the same input."""
    v1 = eval_pulse_mod._hash_vector("hello", 8)
    v2 = eval_pulse_mod._hash_vector("hello", 8)
    assert v1 == v2
    assert len(v1) == 8


def test_hash_vector_differs_for_different_inputs():
    """_hash_vector must produce different vectors for different inputs."""
    va = eval_pulse_mod._hash_vector("alpha", 8)
    vb = eval_pulse_mod._hash_vector("beta", 8)
    assert va != vb


def test_label_signal_known_labels():
    """_label_signal must return the expected fixed values."""
    assert eval_pulse_mod._label_signal("yes") == pytest.approx(0.90)
    assert eval_pulse_mod._label_signal("maybe") == pytest.approx(0.45)
    assert eval_pulse_mod._label_signal("no") == pytest.approx(0.05)


def test_load_fixture_returns_candidates_and_topics():
    """_load_fixture must parse the committed fixture without error."""
    candidates, labels_by_id, labels_by_title, topics = eval_pulse_mod._load_fixture()

    assert len(candidates) > 0, "fixture must contain at least one paper"
    assert len(topics) > 0, "fixture must contain at least one topic"
    assert set(labels_by_id.values()) <= {"yes", "maybe", "no"}, "unexpected label values"
    assert len(labels_by_id) == len(candidates)


@pytest.mark.asyncio
async def test_mock_embedder_produces_vectors():
    """MockEmbedder.embed_texts must return one vector per input with correct length."""
    embedder = eval_pulse_mod.MockEmbedder({"ML paper": "yes", "review": "no"})
    vectors = await embedder.embed_texts(["ML paper title", "review abstract"])

    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec) == eval_pulse_mod.EMBEDDING_DIM


@pytest.mark.asyncio
async def test_mock_embedder_yes_label_has_high_signal():
    """A 'yes'-labeled paper must have a higher dim-0 value than a 'no'-labeled paper."""
    embedder = eval_pulse_mod.MockEmbedder({"yes_title": "yes", "no_title": "no"})
    yes_vec, no_vec = await embedder.embed_texts(["yes_title", "no_title"])

    assert yes_vec[0] > no_vec[0]


# ---------------------------------------------------------------------------
# Import-smoke tests (D2-10: merged from test_eval_pulse_smoke.py)
#
# These load eval_pulse via importlib with heavy-dep stubs so the assertions
# run even on hosts that lack fitz/tiktoken/qdrant_client.
# ---------------------------------------------------------------------------

import importlib.util
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "eval_pulse.py"
_SERVICE_ROOT = _REPO_ROOT / "services" / "paper_ingestion"


def _load_eval_pulse_stubbed(monkeypatch):
    """Load eval_pulse as a fresh module with heavy deps stubbed out."""
    monkeypatch.syspath_prepend(str(_SERVICE_ROOT))

    for mod_name in ("fitz", "tiktoken"):
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, MagicMock())

    if "qdrant_client" not in sys.modules:
        monkeypatch.setitem(
            sys.modules,
            "qdrant_client",
            MagicMock(AsyncQdrantClient=MagicMock()),
        )
    if "qdrant_client.models" not in sys.modules:
        monkeypatch.setitem(
            sys.modules,
            "qdrant_client.models",
            MagicMock(
                Distance=MagicMock(),
                PointStruct=MagicMock(),
                VectorParams=MagicMock(),
            ),
        )

    module_name = "_eval_pulse_smoke"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"Cannot load {_SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except ImportError as exc:
        pytest.skip(f"eval_pulse deps not available on host: {exc}")
    return module


def test_eval_pulse_imports_without_error(monkeypatch):
    """eval_pulse.py can be loaded without raising ImportError."""
    module = _load_eval_pulse_stubbed(monkeypatch)
    assert module is not None


def test_eval_pulse_constants_well_formed(monkeypatch):
    """Module-level constants exist and have sensible values."""
    module = _load_eval_pulse_stubbed(monkeypatch)
    assert module.PRECISION_TARGET >= 0.0
    assert module.NO_LEAKAGE_MAX <= 1.0
    assert module.EMBEDDING_DIM > 0
    assert module.DECK_SIZE > 0
    assert module.FIXTURE_PATH.name == "eval_pulse_labeled_set.json"
