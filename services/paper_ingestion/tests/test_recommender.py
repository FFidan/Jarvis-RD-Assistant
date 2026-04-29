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
from paper_ingestion.recommender import (  # noqa: E402
    _DEFAULT_LIKED_WEIGHT,
    _DEFAULT_PROJECT_WEIGHT,
    _aggregate_to_papers,
    _compute_score,
    _filter_unread,
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
    """_filter_unread returns the set of paper_ids that have no read/archived/starred state."""

    @pytest.mark.asyncio
    async def test_empty_input(self) -> None:
        conn = AsyncMock()
        result = await _filter_unread(conn, [])
        assert result == set()
        conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_unread_ids(self) -> None:
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"id": 1}, {"id": 3}])
        result = await _filter_unread(conn, [1, 2, 3])
        assert result == {1, 3}

    @pytest.mark.asyncio
    async def test_all_read_returns_empty(self) -> None:
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        result = await _filter_unread(conn, [10, 20])
        assert result == set()

    @pytest.mark.asyncio
    async def test_passes_paper_ids_to_query(self) -> None:
        """The paper_ids list must be forwarded to the SQL query as $1."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [5, 6, 7])
        args = conn.fetch.call_args
        # second positional arg is the paper_ids list
        assert [5, 6, 7] in args.args or [5, 6, 7] == args.args[-1]

    @pytest.mark.asyncio
    async def test_starred_papers_remain_eligible_for_recommendation(self) -> None:
        # Sprint 7 B4: starred papers stay eligible. The SQL must NOT exclude
        # status='starred' or COALESCE(starred, FALSE).
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [1])
        sql = conn.fetch.await_args.args[0]
        assert "status = 'read'" in sql
        assert "archived" in sql
        assert "starred" not in sql, "starred state must not gate recommendation eligibility"

    @pytest.mark.asyncio
    async def test_filter_unread_excludes_dismissed(self, test_db_pool):
        """Sprint 8 B3.3: dismissed papers are excluded from candidate set."""
        # Insert a paper
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-dismiss-1', 'arxiv', 'T', '{}', 'http://x') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, status, dismissed) "
                "VALUES ($1, NULL, 'new', TRUE)",
                paper_id,
            )
            result = await _filter_unread(conn, [paper_id])
            assert paper_id not in result, "Dismissed papers must be excluded from candidates"


# ===========================================================================
# 4. refresh_recommendations — integration (fully mocked DB + embedder)
# ===========================================================================

# Re-use the FakeRecord helper defined in conftest.py (auto-loaded by pytest)
# The conftest.py FakeRecord is available via the fake_record fixture, but we
# can also import it directly after conftest adds _SERVICE_ROOT to sys.path.
try:
    from conftest import FakeRecord  # type: ignore[import]
except ImportError:
    # Fallback: minimal dict-with-attr-access substitute
    class FakeRecord(dict):  # type: ignore[no-redef]
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def get(self, key, default=None):
            return super().get(key, default)


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
    # _get_starred_ids: SELECT paper_id FROM paper_user_state WHERE status = 'starred'
    # projects: SELECT name, description FROM projects WHERE status = 'active'
    # _filter_unread: SELECT id FROM papers WHERE ...
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
        count = await refresh_recommendations(app)
        assert count == 0

    @pytest.mark.asyncio
    async def test_no_signals_returns_zero(self) -> None:
        """No starred papers and no active projects → 0 recommendations."""
        app = _build_app(starred_ids=[], projects=[])
        count = await refresh_recommendations(app)
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
        count = await refresh_recommendations(app)
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
        count = await refresh_recommendations(app)
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
        count = await refresh_recommendations(app)
        assert count == 0

    @pytest.mark.asyncio
    async def test_already_read_excluded(self) -> None:
        """Papers present in paper_user_state as read/archived/starred are excluded."""
        # High score but _filter_unread returns empty → means the paper is read/etc.
        app = _build_app(
            starred_ids=[1],
            discover_results=[{"paper_id": 55, "score": 0.95}],
            projects=[],
            unread_ids=[],  # paper 55 is filtered out
        )
        count = await refresh_recommendations(app)
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
        count = await refresh_recommendations(app)
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
        await refresh_recommendations(app)
        conn = app.state.db_pool.acquire.return_value.__aenter__.return_value
        rows = conn.executemany.call_args.args[1]
        modes = rows[0][2]  # 3rd column is modes list
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
        await refresh_recommendations(app)
        conn = app.state.db_pool.acquire.return_value.__aenter__.return_value
        rows = conn.executemany.call_args.args[1]
        pid, score, modes, _explanation = rows[0]
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
        count = await refresh_recommendations(app)
        assert count == n
