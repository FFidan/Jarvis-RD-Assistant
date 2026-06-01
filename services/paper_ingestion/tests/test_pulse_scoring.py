"""Tests for ScoredCandidate immutability.

Verifies that ScoredCandidate is frozen so no caller can accidentally mutate a
candidate that has already passed through the pipeline.
"""

from dataclasses import FrozenInstanceError
from datetime import date

import pytest
from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.pulse.scoring import ScoredCandidate


def _make_paper(title: str = "Test Paper") -> PaperCreate:
    return PaperCreate(
        external_id="arxiv:test-0001",
        source_type=SourceType.ARXIV,
        title=title,
        authors=["Author A"],
        abstract="Test abstract.",
        published_date=date(2025, 1, 1),
        url="https://arxiv.org/abs/test-0001",
    )


def _make_candidate(**kwargs) -> ScoredCandidate:
    defaults = dict(
        paper=_make_paper(),
        signals={"embedding": 0.8, "recency": 0.6},
        llm_relevance=7,
        llm_novelty=6,
        reasoning="Relevant to topic.",
        final_score=0.75,
    )
    defaults.update(kwargs)
    return ScoredCandidate(**defaults)


class TestScoredCandidateFrozen:
    """ScoredCandidate must be immutable after construction."""

    def test_post_construction_field_assignment_raises_frozen_instance_error(self):
        """Assigning to any field after construction must raise FrozenInstanceError."""
        candidate = _make_candidate()
        with pytest.raises(FrozenInstanceError):
            candidate.final_score = 0.99  # type: ignore[misc]

    def test_post_construction_llm_relevance_mutation_raises(self):
        """Assigning to llm_relevance after construction must raise FrozenInstanceError."""
        candidate = _make_candidate()
        with pytest.raises(FrozenInstanceError):
            candidate.llm_relevance = 10  # type: ignore[misc]

    def test_post_construction_reasoning_mutation_raises(self):
        """Assigning to reasoning after construction must raise FrozenInstanceError."""
        candidate = _make_candidate()
        with pytest.raises(FrozenInstanceError):
            candidate.reasoning = "mutated"  # type: ignore[misc]

    def test_construction_still_works_with_all_fields(self):
        """ScoredCandidate can still be constructed normally with all fields."""
        candidate = _make_candidate(
            llm_relevance=8,
            llm_novelty=7,
            reasoning="Good paper.",
            final_score=0.85,
            reasoning_verified=True,
        )
        assert candidate.final_score == 0.85
        assert candidate.llm_relevance == 8
        assert candidate.reasoning_verified is True

    def test_construction_with_none_optional_fields(self):
        """ScoredCandidate can be constructed with None for optional fields."""
        candidate = _make_candidate(
            llm_relevance=None,
            llm_novelty=None,
            reasoning=None,
            final_score=None,
        )
        assert candidate.llm_relevance is None
        assert candidate.final_score is None


def test_pulse_scoring_shape_a_system_prompt_is_non_empty() -> None:
    """Shape A regression: stage2_llm_rerank uses a split-role prompt.

    Confirms that PULSE_SCORING_SYSTEM_PROMPT (the instruction head) is non-empty
    and is distinct from the user-content portion built by build_scoring_prompt.
    """
    from paper_ingestion.pulse.prompts import PULSE_SCORING_SYSTEM_PROMPT, build_scoring_prompt
    from paper_ingestion.pulse import scoring as scoring_module

    assert PULSE_SCORING_SYSTEM_PROMPT, "PULSE_SCORING_SYSTEM_PROMPT must be non-empty"
    assert "relevance scoring assistant" in PULSE_SCORING_SYSTEM_PROMPT
    assert scoring_module.PULSE_SCORING_SYSTEM_PROMPT is PULSE_SCORING_SYSTEM_PROMPT

    messages = build_scoring_prompt(
        topic_context=[],
        positive_examples=[],
        negative_examples=[],
        candidate=_make_paper(),
    )
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[0]["content"] == PULSE_SCORING_SYSTEM_PROMPT
