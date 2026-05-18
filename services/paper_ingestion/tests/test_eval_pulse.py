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
