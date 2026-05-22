"""Unit tests for services/paper_ingestion/app/recommender.py.

Coverage targets:
- Pure scoring: _compute_score weight arithmetic
- Filter helpers: _aggregate_to_papers, _filter_unread
- Integration: refresh_recommendations happy-path with full DB mocking
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import the module under test (recommender has no heavy transitive imports)
# ---------------------------------------------------------------------------
from paper_ingestion.ingestion.recommender import (  # noqa: E402
    _DEFAULT_LIKED_WEIGHT,
    _DEFAULT_PROJECT_WEIGHT,
    _aggregate_to_papers,
    _compute_score,
    _filter_unread,
    _refresh_recommendations_for_user,
    refresh_recommendations,
)

# ===========================================================================
# 1. Pure scoring — _compute_score
# ===========================================================================


class TestComputeScore:
    """Direct arithmetic tests for _compute_score.

    Default weights: liked=0.6, project=0.4.
    """

    def test_both_max(self) -> None:
        """Both signals at 1.0 → score == 1.0."""
        assert _compute_score(
            1.0, 1.0, _DEFAULT_LIKED_WEIGHT, _DEFAULT_PROJECT_WEIGHT
        ) == pytest.approx(1.0)

    def test_both_zero(self) -> None:
        """Both signals at 0.0 → score == 0.0."""
        assert _compute_score(
            0.0, 0.0, _DEFAULT_LIKED_WEIGHT, _DEFAULT_PROJECT_WEIGHT
        ) == pytest.approx(0.0)

    def test_liked_only(self) -> None:
        """Only liked signal → weight=0.6 applied exactly."""
        assert _compute_score(
            1.0, 0.0, _DEFAULT_LIKED_WEIGHT, _DEFAULT_PROJECT_WEIGHT
        ) == pytest.approx(0.6)

    def test_project_only(self) -> None:
        """Only project signal → weight=0.4 applied exactly."""
        assert _compute_score(
            0.0, 1.0, _DEFAULT_LIKED_WEIGHT, _DEFAULT_PROJECT_WEIGHT
        ) == pytest.approx(0.4)

    def test_partial_signals(self) -> None:
        """Partial signals obey weighted sum: 0.5*0.6 + 0.8*0.4 = 0.62."""
        result = _compute_score(0.5, 0.8, _DEFAULT_LIKED_WEIGHT, _DEFAULT_PROJECT_WEIGHT)
        assert result == pytest.approx(0.5 * 0.6 + 0.8 * 0.4)

    def test_custom_weights(self) -> None:
        """Custom weight parameters are respected."""
        assert _compute_score(1.0, 0.0, 0.3, 0.7) == pytest.approx(0.3)
        assert _compute_score(0.0, 1.0, 0.3, 0.7) == pytest.approx(0.7)

    def test_near_tie_ordering_is_stable(self) -> None:
        """Two candidates differing by 1e-9 produce a stable ordering."""
        score_a = _compute_score(0.700_000_001, 0.5, _DEFAULT_LIKED_WEIGHT, _DEFAULT_PROJECT_WEIGHT)
        score_b = _compute_score(0.700_000_000, 0.5, _DEFAULT_LIKED_WEIGHT, _DEFAULT_PROJECT_WEIGHT)
        assert score_a > score_b, "Higher liked signal must yield higher score even for tiny diff"

    def test_score_additive_symmetry(self) -> None:
        """Swapping liked and project with swapped weights should produce the same score."""
        s1 = _compute_score(0.8, 0.3, 0.6, 0.4)
        s2 = _compute_score(0.3, 0.8, 0.4, 0.6)
        assert s1 == pytest.approx(s2)


# ===========================================================================
# 2. _aggregate_to_papers
# ===========================================================================


class TestAggregateToPapers:
    """_aggregate_to_papers collapses chunk-level results to paper-level max scores."""

    def test_empty_input(self) -> None:
        assert _aggregate_to_papers([]) == []

    def test_single_chunk(self) -> None:
        result = _aggregate_to_papers([{"paper_id": 1, "score": 0.75}])
        assert result == [(1, 0.75)]

    def test_max_per_paper(self) -> None:
        """Multiple chunks for the same paper → max score selected."""
        items = [
            {"paper_id": 10, "score": 0.5},
            {"paper_id": 10, "score": 0.9},
            {"paper_id": 10, "score": 0.7},
        ]
        result = dict(_aggregate_to_papers(items))
        assert result[10] == pytest.approx(0.9)

    def test_multiple_papers_independent(self) -> None:
        items = [
            {"paper_id": 1, "score": 0.8},
            {"paper_id": 2, "score": 0.6},
            {"paper_id": 1, "score": 0.4},
        ]
        result = dict(_aggregate_to_papers(items))
        assert result[1] == pytest.approx(0.8)
        assert result[2] == pytest.approx(0.6)

    def test_missing_paper_id_skipped(self) -> None:
        """Items without paper_id are silently ignored."""
        items = [{"score": 0.9}, {"paper_id": 5, "score": 0.5}]
        result = dict(_aggregate_to_papers(items))
        assert 5 in result
        assert None not in result

    def test_missing_score_not_included(self) -> None:
        """Items without score default to 0.0, which is NOT > 0.0, so they are excluded."""
        items = [{"paper_id": 7}]
        result = dict(_aggregate_to_papers(items))
        # score=0.0 is not strictly greater than the default 0.0, so paper_id 7 is skipped
        assert 7 not in result


# ===========================================================================
# 3. _filter_unread
# ===========================================================================


class TestFilterUnread:
    """_filter_unread returns the set of paper_ids eligible for recommendation.

    Eligibility predicate (Phase-A lifecycle contract):
    - state IN ('trash', 'done') → excluded (hard lifecycle gate via paper_user_state.state)
    - recommendation_feedback.signal = 'negative' within 60 days → excluded (L3 hard exclusion)
    - No state row (COALESCE to 'inbox') → eligible
    - state IN ('inbox', 'to_read', 'reading') → eligible
    """

    @pytest.mark.asyncio
    async def test_empty_input(self) -> None:
        conn = AsyncMock()
        result = await _filter_unread(conn, [], user_id=1)
        assert result == set()
        conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_unread_ids(self) -> None:
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"id": 1}, {"id": 3}])
        result = await _filter_unread(conn, [1, 2, 3], user_id=1)
        assert result == {1, 3}

    @pytest.mark.asyncio
    async def test_all_read_returns_empty(self) -> None:
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        result = await _filter_unread(conn, [10, 20], user_id=1)
        assert result == set()

    @pytest.mark.asyncio
    async def test_passes_paper_ids_to_query(self) -> None:
        """The paper_ids list must be forwarded to the SQL query as $1."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [5, 6, 7], user_id=1)
        args = conn.fetch.call_args
        # second positional arg ($1) is the paper_ids list; third ($2) is user_id
        positional = args.args
        assert [5, 6, 7] in positional, f"paper_ids not found in call args: {positional}"

    # E2.PI-rag COLLAPSE: test_lifecycle_predicate_uses_state_column was deleted in W4.
    # E2.PI-rag COLLAPSE: test_negative_feedback_within_60d_predicate_in_sql was deleted in W4.
    # E2.PI-rag COLLAPSE: test_starred_papers_remain_eligible_for_recommendation was deleted in W4.
    # E2.PI-rag COLLAPSE: D3-02 batch deleted in W4.

    # E2.PI-rag COLLAPSE: test_filter_unread_state_predicate (5 parametrized cases) deleted.
    # live_pg tests superseded by contract survivors in test_recommendations_contract.py:
    #   test_filter_unread_excludes_trash_papers (RECS-01)
    #   test_filter_unread_excludes_done_papers (RECS-02)
    #   test_filter_unread_includes_paper_with_no_state_row (RECS-03)
    # The to_read/reading/inbox inclusion is covered by RECS-03 (COALESCE NOT IN trash/done).

    # E2.PI-rag COLLAPSE: test_filter_unread_excludes_negative_feedback_within_60d deleted.
    # live_pg superseded by test_recommendations_contract.py::test_filter_unread_excludes_recent_negative_feedback (RECS-04).

    # E2.PI-rag COLLAPSE: test_filter_unread_includes_paper_after_60d_negative_feedback deleted.
    # live_pg superseded by test_recommendations_contract.py::test_filter_unread_includes_old_negative_feedback (RECS-05).

    # -----------------------------------------------------------------------
    # Multi-tenant isolation tests (W3-T2)
    # -----------------------------------------------------------------------

    # E2.PI-rag COLLAPSE: test_user_id_bound_in_filter_unread_query deleted.
    # SQL-substring / param-position check. Superseded by contract
    # test_recommendations_contract.py::test_filter_unread_cross_user_isolation which exercises
    # the real SQL with real data and asserts on actual result set (strictly stronger).

    # E2.PI-rag COLLAPSE: test_filter_unread_sql_contains_user_id_guard deleted.
    # SQL-substring: asserting 'pus.user_id = $2' in raw SQL text never sent to a real DB.
    # Superseded by test_recommendations_contract.py::test_filter_unread_cross_user_isolation.

    # E2.PI-rag COLLAPSE: test_filter_unread_cross_user_isolation deleted.
    # live_pg superseded by test_recommendations_contract.py::test_filter_unread_cross_user_isolation (RECS-06).

    # E2.PI-rag COLLAPSE: test_filter_unread_done_cross_user_isolation deleted.
    # live_pg; done-cross-user coverage exists in RECS-06 + RECS-02.

    # E2.PI-rag COLLAPSE: test_filter_unread_trash_cross_user_isolation deleted.
    # live_pg; trash-cross-user coverage exists in RECS-06 + RECS-01.


# ===========================================================================
# 3b. _get_starred_ids — multi-tenant isolation (W3-T2)
# ===========================================================================


class TestGetStarredIds:
    """_get_starred_ids must scope results to the given user_id."""

    # E2.PI-rag COLLAPSE: test_user_id_bound_in_query deleted.
    # SQL-substring / param-position check. Superseded by contract
    # test_recommendations_contract.py::test_filter_unread_starred_paper_remains_eligible
    # which exercises the real SQL with real data.

    # E2.PI-rag COLLAPSE: test_sql_contains_user_id_guard deleted.
    # SQL-substring: asserting 'user_id = $1' in raw SQL text. Superseded by contract.

    # E2.PI-rag COLLAPSE: test_starred_cross_user_isolation deleted.
    # live_pg; superseded by test_recommendations_contract.py::test_filter_unread_starred_paper_remains_eligible.

    # E2.PI-rag COLLAPSE: test_starred_boolean_only_drives_starred_ids deleted.
    # live_pg; starred=FALSE/TRUE behavior covered by contract's starred_paper_remains_eligible.


# ===========================================================================
# 4. refresh_recommendations — integration (fully mocked DB + embedder)
# ===========================================================================

from tests.conftest import FakeRecord


def _build_app(
    *,
    config_rows: list | None = None,
    starred_ids: list[int] | None = None,
    projects: list | None = None,
    discover_results: list | None = None,
    search_results: list | None = None,
    unread_ids: list[int] | None = None,
) -> SimpleNamespace:
    """Construct a minimal FastAPI app substitute with mocked db_pool and embedder."""
    if config_rows is None:
        config_rows = []
    if starred_ids is None:
        starred_ids = []
    if projects is None:
        projects = []
    if discover_results is None:
        discover_results = []
    if search_results is None:
        search_results = []
    if unread_ids is None:
        unread_ids = []

    # ---- conn mock --------------------------------------------------------
    conn = AsyncMock()

    # _read_weights: SELECT key, value FROM user_config
    # _get_starred_ids: SELECT paper_id FROM paper_user_state WHERE COALESCE(starred, FALSE)
    # projects: SELECT name, description FROM projects WHERE status = 'active'
    # _filter_unread: SELECT id FROM papers WHERE ... (state not in trash/done, no 60d neg feedback)
    # executemany: upsert

    # We wire conn.fetch with side_effect ordering matching the call sequence inside
    # refresh_recommendations.  Two separate db_pool.acquire() contexts are used:
    #   1st acquire  → _read_weights
    #   2nd acquire  → _get_starred_ids + projects query + _filter_unread
    starred_rows = [FakeRecord({"paper_id": pid}) for pid in starred_ids]
    unread_rows = [FakeRecord({"id": pid}) for pid in unread_ids]
    project_rows = [FakeRecord(p) for p in projects]

    fetch_side_effects = iter([config_rows, starred_rows, project_rows, unread_rows])

    conn.fetch = AsyncMock(side_effect=lambda *_a, **_kw: next(fetch_side_effects))
    conn.executemany = AsyncMock()

    # ---- pool mock --------------------------------------------------------
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    # ---- embedder mock ----------------------------------------------------
    embedder = AsyncMock()
    embedder.discover_from_seeds = AsyncMock(return_value=discover_results)
    embedder.search_similar = AsyncMock(return_value=search_results)

    return SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            embedder=embedder,
        )
    )


class TestRefreshRecommendations:
    """Integration-level tests for refresh_recommendations (DB + embedder fully mocked)."""

    @pytest.mark.asyncio
    async def test_disabled_returns_zero(self) -> None:
        """When recommendation.enabled=false in config, returns 0 without querying embedder."""
        config_rows = [FakeRecord({"key": "recommendation.enabled", "value": False})]
        app = _build_app(config_rows=config_rows)
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == 0

    @pytest.mark.asyncio
    async def test_no_signals_returns_zero(self) -> None:
        """No starred papers and no active projects → 0 recommendations."""
        app = _build_app(starred_ids=[], projects=[])
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == 0

    @pytest.mark.asyncio
    async def test_empty_candidate_list_returns_zero(self) -> None:
        """Embedder returns no results → 0 recommendations saved."""
        app = _build_app(
            starred_ids=[1],
            discover_results=[],
            projects=[],
            unread_ids=[],
        )
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == 0

    @pytest.mark.asyncio
    async def test_single_candidate_saved(self) -> None:
        """One candidate above _MIN_SCORE that is unread → 1 recommendation saved."""
        app = _build_app(
            starred_ids=[1],
            discover_results=[{"paper_id": 99, "score": 0.9}],
            projects=[],
            unread_ids=[99],
        )
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == 1
        # Verify executemany was called with the right paper_id
        exec_call = (
            app.state.db_pool.acquire.return_value.__aenter__.return_value.executemany.call_args
        )
        assert exec_call is not None
        rows = exec_call.args[1]
        assert rows[0][0] == 99  # paper_id is first column

    @pytest.mark.asyncio
    async def test_below_min_score_excluded(self) -> None:
        """Candidates whose weighted score is below _MIN_SCORE=0.25 are excluded."""
        # liked_score=0.3 → score=0.3*0.6=0.18 < 0.25
        app = _build_app(
            starred_ids=[1],
            discover_results=[{"paper_id": 42, "score": 0.3}],
            projects=[],
            unread_ids=[42],
        )
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == 0

    @pytest.mark.asyncio
    async def test_already_read_excluded(self) -> None:
        """Papers filtered out by _filter_unread (state=trash/done or 60d negative feedback) are excluded."""
        # High score but _filter_unread returns empty → paper is in trash/done or has recent negative feedback.
        app = _build_app(
            starred_ids=[1],
            discover_results=[{"paper_id": 55, "score": 0.95}],
            projects=[],
            unread_ids=[],  # paper 55 is filtered out
        )
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == 0

    @pytest.mark.asyncio
    async def test_project_signal_only(self) -> None:
        """Project signal alone (no starred papers) can produce recommendations."""
        app = _build_app(
            starred_ids=[],
            projects=[{"name": "NeRF research", "description": "Neural Radiance Fields"}],
            search_results=[{"paper_id": 77, "score": 0.9}],
            unread_ids=[77],
        )
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == 1

    @pytest.mark.asyncio
    async def test_mode_flags_populated(self) -> None:
        """When only liked signal fires, modes=['liked']; only project → modes=['project']."""
        # liked signal only
        app = _build_app(
            starred_ids=[1],
            discover_results=[{"paper_id": 10, "score": 0.9}],
            projects=[],
            unread_ids=[10],
        )
        await _refresh_recommendations_for_user(app, user_id=1)
        conn = app.state.db_pool.acquire.return_value.__aenter__.return_value
        rows = conn.executemany.call_args.args[1]
        # WS-2D: row tuple is now (paper_id, user_id, score, modes, explanation).
        modes = rows[0][3]
        assert modes == ["liked"]

    @pytest.mark.asyncio
    async def test_both_signals_merged(self) -> None:
        """Paper appearing in both liked and project signals gets combined score."""
        # liked score 0.8 → 0.8*0.6=0.48; project score 0.7 → 0.7*0.4=0.28; total=0.76
        app = _build_app(
            starred_ids=[1],
            discover_results=[{"paper_id": 20, "score": 0.8}],
            projects=[{"name": "ML", "description": "machine learning"}],
            search_results=[{"paper_id": 20, "score": 0.7}],
            unread_ids=[20],
        )
        await _refresh_recommendations_for_user(app, user_id=1)
        conn = app.state.db_pool.acquire.return_value.__aenter__.return_value
        rows = conn.executemany.call_args.args[1]
        # WS-2D: row tuple is now (paper_id, user_id, score, modes, explanation).
        pid, _user_id, score, modes, _explanation = rows[0]
        assert pid == 20
        assert score == pytest.approx(0.8 * 0.6 + 0.7 * 0.4)
        assert set(modes) == {"liked", "project"}

    @pytest.mark.asyncio
    async def test_max_recommendations_not_exceeded(self) -> None:
        """Score threshold + _filter_unread filter; returned count ≤ number of unread passed."""
        n = 5
        discover = [{"paper_id": i, "score": 0.9} for i in range(n)]
        unread = list(range(n))
        app = _build_app(
            starred_ids=[999],
            discover_results=discover,
            projects=[],
            unread_ids=unread,
        )
        count = await _refresh_recommendations_for_user(app, user_id=1)
        assert count == n


class TestRefreshRecommendationsFanout:
    """WS-CROSS-USER: the nightly (user_id=None) path iterates real users."""

    @pytest.mark.asyncio
    async def test_per_user_function_rejects_none_user(self) -> None:
        """The per-user core asserts a concrete user_id (no NULL-owned writes)."""
        app = _build_app()
        with pytest.raises(AssertionError):
            # Deliberately invalid: verifies the per-user core rejects a NULL
            # user_id (no NULL-owned writes). The type error is the point.
            await _refresh_recommendations_for_user(app, user_id=None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_nightly_path_fans_out_over_non_deleted_users(self, monkeypatch) -> None:
        """refresh_recommendations(app) runs the per-user logic once per user."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = ctx
        app = SimpleNamespace(state=SimpleNamespace(db_pool=pool, embedder=AsyncMock()))

        seen: list[int] = []

        async def _fake_per_user(_app, user_id):
            seen.append(user_id)
            return 3

        monkeypatch.setattr(
            "paper_ingestion.ingestion.recommender._refresh_recommendations_for_user",
            _fake_per_user,
        )

        total = await refresh_recommendations(app)

        users_sql = conn.fetch.await_args.args[0]
        assert "FROM users" in users_sql and "deleted_at IS NULL" in users_sql, (
            f"nightly path must enumerate non-deleted users; got:\n{users_sql!r}"
        )
        assert seen == [1, 2]
        assert total == 6


# ===========================================================================
# 5. _read_weights — user-row-wins precedence (M-1 / PI-6)
# ===========================================================================


# Cluster 9 deletion (2026-05-22): TestReadWeightsPrecedence class superseded by
# test_recommendations_contract.py::test_c9_03_read_weights_user_row_wins_over_global.
