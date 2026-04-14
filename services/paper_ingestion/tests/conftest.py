"""Shared test fixtures for paper_ingestion tests.

Loaded automatically by pytest before any test file in this directory.
Module stubs MUST be at module level (not in fixtures) because they need
to be installed before any ``import app.*`` triggers transitive imports
of heavy dependencies that are only available inside Docker.
"""

import json
import math
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Path setup (replaces per-file sys.path.insert boilerplate)
# ---------------------------------------------------------------------------
_SERVICE_ROOT = str(Path(__file__).resolve().parents[1])
# In Docker the service is mounted at /app (only 2 parents above conftest.py).
# On the host the path has more components.  Use try/except to stay portable.
try:
    _JARVIS_COMMON = str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common")
except IndexError:
    _JARVIS_COMMON = None  # type: ignore[assignment]
for p in (_SERVICE_ROOT, _JARVIS_COMMON):
    if p is not None and p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# 2. Module stubs for Docker-only dependencies
#    Guards ensure existing per-file stubs are not overwritten.
# ---------------------------------------------------------------------------
# Pre-import apscheduler.triggers.cron so that per-file stubs in
# test_pulse_scheduler.py cannot replace the real CronTrigger (needed by
# the _validate_cron validator in app.routers.settings).
import apscheduler.triggers.cron  # noqa: F401, E402

if "fitz" not in sys.modules:
    sys.modules["fitz"] = MagicMock()

for _marker_mod in ("marker", "marker.converters", "marker.converters.pdf", "marker.models"):
    if _marker_mod not in sys.modules:
        sys.modules[_marker_mod] = MagicMock()

if "tiktoken" not in sys.modules:
    _fake_tiktoken = types.ModuleType("tiktoken")

    class _FakeEncoding:
        def encode(self, text):
            return list(text)

        def decode(self, tokens):
            return "".join(tokens)

    _fake_tiktoken.get_encoding = lambda _name: _FakeEncoding()
    sys.modules["tiktoken"] = _fake_tiktoken

if "qdrant_client" not in sys.modules:
    _fake_qdrant = types.ModuleType("qdrant_client")
    _fake_qdrant.AsyncQdrantClient = MagicMock()
    sys.modules["qdrant_client"] = _fake_qdrant

if "qdrant_client.models" not in sys.modules:
    from types import SimpleNamespace

    _fake_qm = types.ModuleType("qdrant_client.models")
    for _attr in (
        "Distance",
        "FieldCondition",
        "Filter",
        "MatchAny",
        "MatchValue",
        "PointIdsList",
        "PointStruct",
        "VectorParams",
        "RecommendInput",
        "RecommendQuery",
        "RecommendStrategy",
    ):
        setattr(_fake_qm, _attr, MagicMock())
    _fake_qm.Distance = SimpleNamespace(COSINE="cosine")
    _fake_qm.RecommendStrategy = SimpleNamespace(AVERAGE_VECTOR="average")
    sys.modules["qdrant_client.models"] = _fake_qm

if "rapidfuzz" not in sys.modules:
    _fake_rapidfuzz = types.ModuleType("rapidfuzz")
    _fake_rapidfuzz.fuzz = MagicMock()
    sys.modules["rapidfuzz"] = _fake_rapidfuzz

try:
    import python_multipart  # noqa: F401
except ImportError:
    for _mod in ("python_multipart", "multipart", "multipart.multipart"):
        if _mod not in sys.modules:
            sys.modules[_mod] = MagicMock()

# ---------------------------------------------------------------------------
# 3. FakeRecord + shared fixtures
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Unified asyncpg.Record substitute: dict[], .attr, .keys(), .get(), .values()."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _make_pool_and_conn():
    """Create mock asyncpg Pool + Connection with transaction support."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord


# ---------------------------------------------------------------------------
# 4. Pulse subsystem fixture helpers (Stream F0 — consumed by Streams A/B/C/D)
# ---------------------------------------------------------------------------


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


def make_pulse_rating_row(
    paper_id: int = 42,
    rating: str = "up",
    source: str = "pulse",
) -> FakeRecord:
    """Return a FakeRecord matching the pulse_ratings schema.

    Parameters
    ----------
    paper_id:
        FK to papers.id.
    rating:
        One of ``'up'``, ``'down'``, ``'save'``, ``'dismiss'``, ``'open'``.
    source:
        Origin of the rating; defaults to ``'pulse'``. Reserved for future
        non-Pulse rating sources (e.g. library UI thumbs).
    """
    return FakeRecord(
        {
            "id": 1,
            "paper_id": paper_id,
            "rating": rating,
            "source": source,
            "created_at": "2024-01-15T08:30:00+00:00",
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


def fake_embedding_vector(dim: int = 768) -> list[float]:
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
