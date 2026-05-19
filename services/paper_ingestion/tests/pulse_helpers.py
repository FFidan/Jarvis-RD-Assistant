"""Pulse-subsystem test fixture helpers (D9-07 — extracted from conftest.py).

These helpers are pulse-specific and are only used by test_pulse_deck.py and
test_pulse_profile.py.  Keeping them here (rather than in the shared conftest)
reduces the 724-line conftest and makes the scope of each helper explicit.

Both callers import them directly from this module:

    from tests.pulse_helpers import make_pulse_deck_row, ...

The conftest.py also re-exports them so any legacy
``from tests.conftest import make_pulse_deck_row`` still resolves correctly.
"""

from __future__ import annotations

import json
import math

from jarvis_common.testing import FakeRecord


def make_pulse_deck_row(
    deck_date: str = "2024-01-15",
    card_count: int = 10,
    stats: dict | None = None,
) -> FakeRecord:
    """Return a FakeRecord matching the pulse_decks schema.

    Parameters
    ----------
    deck_date:
        ISO date string (YYYY-MM-DD) or a datetime.date for ``deck_date``.
    card_count:
        Number of cards in the deck.
    stats:
        Optional JSONB stats dict (candidate_count, llm_calls, duration_s, etc.).
    """
    return FakeRecord(
        {
            "id": 1,
            "deck_date": deck_date,
            "card_count": card_count,
            "generated_at": "2024-01-15T04:00:00+00:00",
            "stats": stats if stats is not None else {},
        }
    )


def make_pulse_card_row(
    deck_id: int = 1,
    paper_id: int = 42,
    rank: int = 1,
    score: float = 0.85,
    reasoning: str = "Highly relevant to your active topics.",
    signals: dict | None = None,
) -> FakeRecord:
    """Return a FakeRecord matching the pulse_cards schema.

    Parameters
    ----------
    deck_id:
        FK to pulse_decks.id.
    paper_id:
        FK to papers.id.
    rank:
        1-based rank within the deck (lower = more relevant).
    score:
        Composite score in [0, 1].
    reasoning:
        One-sentence LLM explanation for inclusion.
    signals:
        Per-signal score breakdown, e.g. ``{"embedding": 0.82, "topic": 0.74}``.
    """
    return FakeRecord(
        {
            "id": rank,
            "deck_id": deck_id,
            "paper_id": paper_id,
            "rank": rank,
            "score": score,
            "llm_relevance": 8,
            "llm_novelty": 6,
            "reasoning": reasoning,
            "signals": signals if signals is not None else {"embedding": 0.82, "topic": 0.74},
            "created_at": "2024-01-15T04:00:01+00:00",
        }
    )


def make_pdf_resolution_row(
    doi: str | None = "10.1234/example",
    arxiv_id: str | None = None,
    resolved_url: str | None = "https://arxiv.org/pdf/2401.00001",
    resolver_name: str = "arxiv",
) -> FakeRecord:
    """Return a FakeRecord matching the pdf_resolutions schema.

    Parameters
    ----------
    doi:
        Canonical DOI or None for arXiv-only papers.
    arxiv_id:
        arXiv identifier or None for DOI-only papers.
    resolved_url:
        PDF URL if resolution succeeded; None if all resolvers failed
        (cached failure marker).
    resolver_name:
        Which resolver produced the result (``'arxiv'``, ``'unpaywall'``,
        ``'core'``, or ``'failed'``).
    """
    return FakeRecord(
        {
            "id": 1,
            "doi": doi,
            "arxiv_id": arxiv_id,
            "resolved_url": resolved_url,
            "resolver_name": resolver_name,
            "resolved_at": "2024-01-15T04:05:00+00:00",
        }
    )


def fake_embedding_vector(dim: int = 1024) -> list[float]:
    """Return a deterministic unit-ish embedding vector of length ``dim``.

    Values cycle through a simple pattern so tests are reproducible without
    importing numpy.  For callers that need a numpy array, wrap with
    ``np.array(fake_embedding_vector())``.
    """
    # Deterministic, non-trivial values: sin(i / dim * pi) normalised
    raw = [math.sin(i / max(dim, 1) * math.pi) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def fake_llm_score_response(
    relevance: int = 7,
    novelty: int = 5,
    reasoning: str = "This paper directly addresses your active research topics.",
) -> str:
    """Return the JSON string a mocked LLM would produce for Pulse Stage 2 scoring.

    Parameters
    ----------
    relevance:
        Integer 1-10 relevance score.
    novelty:
        Integer 1-10 novelty score.
    reasoning:
        One-sentence explanation string.
    """
    return json.dumps(
        {
            "relevance": relevance,
            "novelty": novelty,
            "reasoning": reasoning,
        }
    )
