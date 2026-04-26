"""Focused property test for quote normalization invariants."""

from __future__ import annotations

from hypothesis import assume, given, settings
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


@given(
    quote=st.text(
        alphabet=st.characters(whitelist_categories=["L", "N", "Zs"]),
        min_size=10,
        max_size=80,
    ),
    noise_left=st.text(max_size=200),
    noise_right=st.text(max_size=200),
)
@settings(max_examples=100, deadline=None)
def test_verify_quote_finds_exact_substring(quote: str, noise_left: str, noise_right: str):
    """QuoteVerifier must verify a quote that is an exact substring of the source text.

    When the quote appears verbatim (modulo normalization) inside source, the
    verifier must return verified=True with match_score >= 0.9.
    """
    assume(quote.strip())  # skip degenerate whitespace-only quotes
    source = f"{noise_left} {quote} {noise_right}"
    result = QuoteVerifier().verify_quote(quote, source, [])
    assert result.verified is True, (
        f"Expected verified=True for exact substring quote={quote!r} in source"
    )
    assert result.match_score is not None and result.match_score >= 0.9, (
        f"Expected match_score >= 0.9 for exact match, got {result.match_score}"
    )
