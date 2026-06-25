"""Characterization tests for pulse/job.py::run_pulse — pre-decomposition snapshot.

These tests pin the observable shape and degraded-path behaviour of run_pulse
before helper functions are extracted into _orchestrator_phases.py.  The same
tests MUST pass byte-identically after extraction; any divergence signals a
regression.

Scope: smoke-level (2–4 assertions each).  Exhaustive pulse coverage lives in
the existing contract/test_pulse_contract.py suite.

Stats dict keys verified against docs/contracts/02-pulse.md §7:
  candidate_count, stage1_survivors, stage2_scored, llm_calls, duration_s,
  last_error, degraded_reason, deck_date, card_count, source_counts,
  source_diagnostics, classifier, classifier_training_enqueued, verification_stats.

Verified identifiers:
  pulse.job.run_pulse                job.py:100 — async pipeline, returns stats dict
  pulse.job._zero_candidate_degraded_reason  job.py:81 — builds degraded string
  docs/contracts/02-pulse.md §7     — 14-key stats dict contract
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import SharedConnPool

# Verified: services/paper_ingestion/paper_ingestion/pulse/job.py:99 (run_pulse)
pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Expected stats keys per docs/contracts/02-pulse.md §7
# ---------------------------------------------------------------------------

_CONTRACT_STATS_KEYS = frozenset(
    {
        "candidate_count",
        "stage1_survivors",
        "stage2_scored",
        "llm_calls",
        "duration_s",
        "last_error",
        "degraded_reason",
        "deck_date",
        "card_count",
        "source_counts",
        "source_diagnostics",
        "classifier",
        "classifier_training_enqueued",
        "verification_stats",
    }
)

# ---------------------------------------------------------------------------
# Shared fixture: far-future deck date to avoid collision with production rows
# ---------------------------------------------------------------------------

_DECK_DATE_CHAR = datetime(2098, 12, 1, 4, 0, tzinfo=UTC)


def _minimal_profile() -> MagicMock:
    """Minimal profile mock that satisfies run_pulse without hitting real sources."""
    return MagicMock(
        topics=[],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={"embedding": 1.0},
        deck_size=5,
        stage2_top_k=10,
        liked_paper_ids=[],
        recent_positive_titles=[],
        recent_negative_titles=[],
        lookback_days=7,
    )


# ---------------------------------------------------------------------------
# Shared patch context: zero-candidate pipeline collaborators
# ---------------------------------------------------------------------------


@contextmanager
def _zero_candidate_patches():
    """Patch all pipeline collaborators to produce zero candidates.

    Yields nothing — tests use it purely to suppress I/O and LLM calls.
    Both characterization tests share the exact same 5-patch combination;
    extracting it here avoids ~40 LOC of duplication.
    """
    with (
        patch(
            "paper_ingestion.pulse.job.load_profile",
            AsyncMock(return_value=_minimal_profile()),
        ),
        patch(
            "paper_ingestion.pulse.job.discover_candidates",
            AsyncMock(return_value=([], {}, {})),
        ),
        patch(
            "paper_ingestion.pulse.job.stage1_embedding_filter",
            AsyncMock(return_value=[]),
        ),
        patch(
            "paper_ingestion.pulse.job.stage3_combine",
            AsyncMock(return_value=[]),
        ),
        patch("paper_ingestion.pulse.job.assemble_deck", MagicMock(return_value=[])),
    ):
        yield


# ---------------------------------------------------------------------------
# Test 1 — stats dict has all 14 contract keys when zero candidates returned
# ---------------------------------------------------------------------------


async def test_run_pulse_returns_stats_with_all_contract_keys(
    contract_conn,
    contract_two_users,
):
    """run_pulse stats dict contains every key listed in 02-pulse.md §7.

    Uses patched collaborators so no real LLM / embedder / sources are needed.
    Zero candidates → degraded path (card_count=0, deck_date set, all keys present).

    Verified: pulse/job.py:133-147 (stats dict initialisation, 14 keys),
              pulse/job.py:376-378 (deck_date + degraded_reason populated before return),
              docs/contracts/02-pulse.md §7 (14-key contract).
    """
    from paper_ingestion.pulse.job import run_pulse

    pool = SharedConnPool(contract_conn)
    user_id = contract_two_users.user_a_id

    with _zero_candidate_patches():
        stats = await run_pulse(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
            now=_DECK_DATE_CHAR,
            user_id=user_id,
        )

    missing = _CONTRACT_STATS_KEYS - set(stats.keys())
    assert not missing, (
        f"run_pulse stats dict is missing required contract keys: {sorted(missing)}. "
        f"Present keys: {sorted(stats.keys())}"
    )
    # Smoke-check types for a representative subset
    assert isinstance(stats["candidate_count"], int)
    assert isinstance(stats["stage1_survivors"], int)
    assert isinstance(stats["duration_s"], float | int)
    assert isinstance(stats["deck_date"], str)


# ---------------------------------------------------------------------------
# Test 2 — zero candidates → degraded_reason is non-None
# ---------------------------------------------------------------------------


async def test_run_pulse_zero_candidates_returns_degraded_reason(
    contract_conn,
    contract_two_users,
):
    """When all sources return zero candidates, degraded_reason must be non-None.

    Contract: 02-pulse.md §9 invariant 9 + §7 degraded_reason key.
    Verified: pulse/job.py:189-191 (_zero_candidate_degraded_reason called when
              candidates=[] and last_error is None).
    """
    from paper_ingestion.pulse.job import run_pulse

    pool = SharedConnPool(contract_conn)
    user_id = contract_two_users.user_a_id

    # Ensure a unique deck date distinct from test 1 to avoid UPSERT collision
    deck_date_t2 = datetime(2098, 12, 2, 4, 0, tzinfo=UTC)

    with _zero_candidate_patches():
        stats = await run_pulse(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
            now=deck_date_t2,
            user_id=user_id,
        )

    # Zero candidates with no fatal error must set a human-readable degraded_reason
    assert stats["degraded_reason"] is not None, (
        "run_pulse with zero candidates must set stats['degraded_reason'] to a non-None string. "
        f"Got: degraded_reason={stats['degraded_reason']!r}, last_error={stats['last_error']!r}"
    )
    assert isinstance(stats["degraded_reason"], str)
    assert len(stats["degraded_reason"]) > 0
    # The zero-candidate reason must mention candidates (contract wording)
    assert "candidates" in stats["degraded_reason"].lower(), (
        f"degraded_reason should reference 'candidates'; got: {stats['degraded_reason']!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — classifier_training_enqueued=True path + verification_stats shape
# ---------------------------------------------------------------------------


async def test_run_pulse_classifier_training_enqueue_success_path(
    contract_conn,
    contract_two_users,
    monkeypatch,
):
    """When defer_async succeeds, stats[classifier_training_enqueued]=True.

    Also pins the verification_stats sub-dict shape (per 02-pulse.md §7).

    Coverage gap closed: the other two characterization tests pass ctx=None to
    run_pulse, which structurally skips the `if ctx:` guard at job.py:502 and
    therefore never reaches the `stats["classifier_training_enqueued"] = True`
    assignment at job.py:511. This test fabricates a minimal ctx stub and patches
    KIND_TO_TASK so defer_async completes successfully, exercising that branch.

    Verified: pulse/job.py:502-515 (ctx guard + defer_async success branch),
              pulse/job.py:524-529 (verification_stats dict always set),
              docs/contracts/02-pulse.md §7 (14-key contract).
    """
    from paper_ingestion.pulse.job import run_pulse

    pool = SharedConnPool(contract_conn)
    user_id = contract_two_users.user_a_id

    # Fabricate a minimal ctx that satisfies the `if ctx:` guard.
    # Both update_progress and is_cancelled are awaited by run_pulse; use
    # AsyncMock so Python doesn't raise "object MagicMock can't be used in
    # 'await' expression".  is_cancelled returns False so the pipeline
    # continues normally rather than raising CancelledError.
    mock_ctx = MagicMock()
    mock_ctx.update_progress = AsyncMock(return_value=None)
    mock_ctx.is_cancelled = AsyncMock(return_value=False)

    # Stub the KIND_TO_TASK entry so defer_async returns successfully.
    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "paper_ingestion.pulse.job.KIND_TO_TASK",
        {"pulse.train_classifier": mock_task},
    )

    deck_date_t3 = datetime(2098, 12, 3, 4, 0, tzinfo=UTC)

    with _zero_candidate_patches():
        stats = await run_pulse(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
            now=deck_date_t3,
            user_id=user_id,
            ctx=mock_ctx,
        )

    # Primary assertion: successful defer_async → enqueued=True
    assert stats["classifier_training_enqueued"] is True, (
        "run_pulse with a successful defer_async must set "
        f"stats['classifier_training_enqueued']=True; got: {stats['classifier_training_enqueued']!r}"
    )

    # Pin verification_stats sub-dict shape (always set, regardless of ctx)
    assert "verification_stats" in stats, (
        "run_pulse must always set stats['verification_stats'] (job.py:524-529)"
    )
    vs = stats["verification_stats"]
    assert set(vs.keys()) == {"pass_rate", "total", "passed", "failed"}, (
        f"verification_stats must have exactly keys pass_rate/total/passed/failed; got: {set(vs.keys())}"
    )
    assert isinstance(vs["pass_rate"], float | int)
    assert isinstance(vs["total"], int)
    assert isinstance(vs["passed"], int)
    assert isinstance(vs["failed"], int)


# ---------------------------------------------------------------------------
# Test 4 — stage-2 schema-echo (all llm_relevance=None) degrades end-to-end
#          and persists the degraded_reason to the pulse_decks row
# ---------------------------------------------------------------------------


def _unscored_candidate():
    """A stage-1 survivor with no LLM relevance — the schema-echo all-fail outcome."""
    from datetime import date

    from paper_ingestion.models import PaperCreate, SourceType
    from paper_ingestion.pulse.scoring import ScoredCandidate

    paper = PaperCreate(
        external_id="arxiv:seam-echo-0001",
        source_type=SourceType.ARXIV,
        title="Schema Echo Paper",
        authors=["Author A"],
        abstract="Abstract.",
        published_date=date(2025, 1, 1),
        url="https://arxiv.org/abs/seam-echo-0001",
    )
    return ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.7, "recency": 0.5},
        llm_relevance=None,
        llm_novelty=None,
        reasoning=None,
        final_score=0.6,
    )


async def test_run_pulse_stage2_schema_echo_persists_degraded_reason(
    contract_conn,
    contract_two_users,
):
    """All stage-2 cards schema-echo (llm_relevance=None) → degraded deck is persisted.

    Drives the full run_pulse pipeline end-to-end against the real contract DB
    (which the _run_stage2 unit tests never reach): stage-1 yields one survivor,
    stage-2 returns it with llm_relevance=None (simulating the per-card schema
    echo), so llm_calls==0 and the degradation-threshold branch fires. Strict
    mode is off (default), so the pipeline does NOT raise — it produces a deck,
    records a non-null degraded_reason, and writes a pulse_decks row carrying it.

    Verified: pulse/job.py:338-349 (llm_calls counted from llm_relevance,
              degradation-threshold warning), pulse/job.py:284-287
              (_persist_pipeline threads stats['degraded_reason']),
              pulse/deck.py:73-89 (pulse_decks UPSERT with degraded_reason).
    """
    from paper_ingestion.pulse.job import run_pulse

    pool = SharedConnPool(contract_conn)
    user_id = contract_two_users.user_a_id

    # Far-future, distinct from sibling tests, to avoid UPSERT collisions.
    deck_date_t4 = datetime(2098, 12, 4, 4, 0, tzinfo=UTC)

    survivors = [_unscored_candidate()]

    with (
        patch(
            "paper_ingestion.pulse.job.load_profile",
            AsyncMock(return_value=_minimal_profile()),
        ),
        patch(
            "paper_ingestion.pulse.job.discover_candidates",
            AsyncMock(return_value=(list(survivors), {}, {})),
        ),
        patch(
            "paper_ingestion.pulse.job.stage1_embedding_filter",
            AsyncMock(return_value=list(survivors)),
        ),
        # stage-2 echoes the stage-1 list back unscored (llm_relevance stays None)
        patch(
            "paper_ingestion.pulse.job.stage2_llm_rerank",
            AsyncMock(return_value=list(survivors)),
        ),
        # avoid the real per-role num_ctx DB lookup inside _run_stage2
        patch(
            "paper_ingestion.pulse.job.effective_num_ctx",
            AsyncMock(return_value=4096),
        ),
        patch(
            "paper_ingestion.pulse.job.stage3_combine",
            AsyncMock(return_value=list(survivors)),
        ),
        patch(
            "paper_ingestion.pulse.job.assemble_deck",
            MagicMock(return_value=list(survivors)),
        ),
    ):
        stats = await run_pulse(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
            now=deck_date_t4,
            user_id=user_id,
        )

    # Every stage-2 card was unscored → no real LLM calls counted.
    assert stats["llm_calls"] == 0, (
        f"all-None stage-2 scores must yield llm_calls==0; got {stats['llm_calls']!r}"
    )
    # The degradation-threshold branch must record an honest degraded_reason.
    assert stats["degraded_reason"] is not None, (
        "stage-2 schema-echo must set a non-null degraded_reason; "
        f"got degraded_reason={stats['degraded_reason']!r}, last_error={stats['last_error']!r}"
    )

    # The degraded deck must be persisted with its reason — the unit tests never
    # exercise this DB write. The paper row is absent in the contract DB, so
    # card_count may be 0, but the pulse_decks row is still written.
    row = await contract_conn.fetchrow(
        """
        SELECT degraded_reason
        FROM pulse_decks
        WHERE deck_date = $1
          AND user_id IS NOT DISTINCT FROM $2
        """,
        deck_date_t4.date(),
        user_id,
    )
    assert row is not None, "run_pulse must persist a pulse_decks row for the degraded deck"
    assert row["degraded_reason"] is not None, (
        "the persisted pulse_decks row must carry the degraded_reason; got NULL"
    )
