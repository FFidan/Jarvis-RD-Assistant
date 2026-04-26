"""Focused property test for quote normalization invariants."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from paper_ingestion.verification import QuoteVerifier


@given(
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        min_size=1,
        max_size=120,
    )
)
@settings(max_examples=50, deadline=None)
def test_quote_verifier_normalization_is_idempotent(text: str):
    """Normalizing already-normalized text should not change it further."""
    verifier = QuoteVerifier()

    normalized = verifier._normalize(text)

    assert verifier._normalize(normalized) == normalized
