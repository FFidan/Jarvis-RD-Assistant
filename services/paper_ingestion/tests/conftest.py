"""Shared test fixtures for paper_ingestion tests.

Loaded automatically by pytest before any test file in this directory.
All runtime dependencies (fitz, tiktoken, qdrant_client, rapidfuzz, marker,
sentence_transformers, apscheduler) are installed on the host venv — no
module stubs are needed.
"""

import json
import math
import os
import shutil
import subprocess
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

# Pre-import apscheduler.triggers.cron so that per-file stubs in
# test_pulse_scheduler.py cannot replace the real CronTrigger (needed by
# the _validate_cron validator in app.routers.settings).
import apscheduler.triggers.cron  # noqa: F401
import jarvis_common.jobs as _jobs_module
import pytest

# ---------------------------------------------------------------------------
# Live PostgreSQL fixture
# ---------------------------------------------------------------------------


def _docker(
    args: list[str],
    *,
    check: bool = True,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a Docker CLI command for opt-in live PostgreSQL tests."""
    return subprocess.run(
        ["docker", *args],
        check=check,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


@pytest.fixture()
def live_pg_dsn() -> str:
    """Return an asyncpg DSN for a disposable PostgreSQL 16 Docker container.

    The fixture is opt-in because it starts a real container. Set
    ``JARVIS_RUN_LIVE_PG=1`` and run tests marked ``live_pg`` to exercise it.
    """
    if os.environ.get("JARVIS_RUN_LIVE_PG") != "1":
        pytest.skip("set JARVIS_RUN_LIVE_PG=1 to run Docker-backed live PostgreSQL tests")
    if shutil.which("docker") is None:
        pytest.fail("Docker CLI is required for JARVIS_RUN_LIVE_PG=1 live PostgreSQL tests")

    container = f"jarvis-rd-live-pg-{uuid.uuid4().hex[:12]}"
    password = f"jarvis-test-{uuid.uuid4().hex}"
    image = os.environ.get("JARVIS_LIVE_PG_IMAGE", "postgres:16.8")
    _docker(
        [
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "-e",
            "POSTGRES_DB=jarvis",
            "-e",
            "POSTGRES_USER=jarvis",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            "127.0.0.1::5432",
            image,
        ]
    )
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready = _docker(
                ["exec", container, "pg_isready", "-U", "jarvis", "-d", "jarvis"],
                check=False,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            logs = _docker(["logs", container], check=False, timeout=10)
            pytest.fail(f"PostgreSQL container did not become ready:\n{logs.stdout}{logs.stderr}")

        port_result = _docker(["port", container, "5432/tcp"])
        host_port = port_result.stdout.strip().rsplit(":", maxsplit=1)[-1]
        yield f"postgresql://jarvis:{password}@127.0.0.1:{host_port}/jarvis"
    finally:
        _docker(["rm", "-f", container], check=False, timeout=10)


# ---------------------------------------------------------------------------
# _HANDLERS isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _reset_job_handlers():
    """Snapshot and restore jarvis_common.jobs._HANDLERS around each test.

    Use this fixture in any test that registers new job handlers, to prevent
    cross-test _HANDLERS state pollution.  Never rely on global _HANDLERS state
    across tests — always opt-in to this fixture when your test touches handler
    registration.
    """
    snapshot = dict(_jobs_module._HANDLERS)
    yield
    _jobs_module._HANDLERS.clear()
    _jobs_module._HANDLERS.update(snapshot)


# ---------------------------------------------------------------------------
# FakeRecord + shared fixtures
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
