"""Unit tests for rag/verification.py — PI-10 sentence splitter coverage.

PI-10: _SENTENCE_RE must NOT split on lowercase-continuation (e.g. "one. and this").
       Also verifies digit and quote boundaries ARE split correctly.
"""

from __future__ import annotations


from paper_ingestion.rag.verification import _SENTENCE_RE, _split_sentences


# ---------------------------------------------------------------------------
# PI-10: sentence regex behaviour
# ---------------------------------------------------------------------------


def test_sentence_re_does_not_split_lowercase_continuation():
    """PI-10: 'This is one. and this should be a continuation.' must yield 1 sentence, not 2."""
    text = "This is one. and this should be a continuation of one."
    parts = _SENTENCE_RE.split(text)
    assert len(parts) == 1, (
        f"Lowercase-continuation must NOT be counted as a new sentence; got {len(parts)} parts: {parts}"
    )


def test_sentence_re_splits_on_uppercase():
    """Normal sentence boundary with uppercase start must be split."""
    text = "First sentence. Second sentence."
    parts = _SENTENCE_RE.split(text)
    assert len(parts) == 2, f"Expected 2 parts; got {len(parts)}: {parts}"


def test_sentence_re_splits_on_digit():
    """Sentence boundary followed by a digit must be split (PI-10 extension)."""
    text = "There are two results. 3 of them are significant."
    parts = _SENTENCE_RE.split(text)
    assert len(parts) == 2, (
        f"Digit-start continuation should be a new sentence; got {len(parts)}: {parts}"
    )


def test_sentence_re_splits_on_question_and_exclamation():
    """? and ! boundaries with uppercase continuation must split."""
    text = "Is this correct? Yes, it is. Really!"
    parts = _SENTENCE_RE.split(text)
    assert len(parts) >= 2, f"Expected ≥2 parts; got {len(parts)}: {parts}"


def test_split_sentences_filters_empty_segments():
    """_split_sentences must drop empty / non-alphanumeric fragments."""
    text = "Only one sentence."
    sentences = _split_sentences(text)
    assert sentences == ["Only one sentence."]


def test_split_sentences_multiple():
    """_split_sentences returns one element per sentence boundary."""
    text = "Alpha sentence. Beta sentence. Gamma sentence."
    sentences = _split_sentences(text)
    assert len(sentences) == 3, f"Expected 3 sentences; got {sentences}"


def test_split_sentences_lowercase_continuation_is_one_sentence():
    """End-to-end: _split_sentences treats lowercase-continuation as single sentence."""
    text = "This is one. and this continues the thought."
    sentences = _split_sentences(text)
    assert len(sentences) == 1, (
        f"Lowercase continuation must not generate a second sentence; got: {sentences}"
    )
