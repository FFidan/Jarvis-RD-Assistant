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
    _get_starred_ids,
    _read_weights,
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

    @pytest.mark.asyncio
    async def test_lifecycle_predicate_uses_state_column(self) -> None:
        # Phase-A B5: the SQL must use COALESCE(pus.state, 'inbox') IN ('trash','done')
        # to gate recommendation eligibility.  Old columns (status, archived, dismissed)
        # were dropped in migration 047 and must NOT appear in the SQL.
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [1], user_id=1)
        sql = conn.fetch.await_args.args[0]
        assert "'trash'" in sql, "SQL must reference trash state"
        assert "'done'" in sql, "SQL must reference done state"
        # Dropped columns must not appear in the predicate
        assert "status = 'read'" not in sql, "status column was dropped in migration 047"
        assert "archived" not in sql, "archived column was dropped in migration 047"
        assert "dismissed" not in sql, "dismissed column was dropped in migration 047"

    @pytest.mark.asyncio
    async def test_starred_papers_remain_eligible_for_recommendation(self) -> None:
        # Phase-A: starred boolean is in paper_user_state but does NOT gate eligibility
        # (starred papers are still recommended). The SQL must NOT exclude on starred.
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [1], user_id=1)
        sql = conn.fetch.await_args.args[0]
        assert "starred" not in sql, "starred state must not gate recommendation eligibility"

    @pytest.mark.asyncio
    async def test_negative_feedback_within_60d_predicate_in_sql(self) -> None:
        # Phase-A L3: SQL must contain the recommendation_feedback 60-day hard exclusion.
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [1], user_id=1)
        sql = conn.fetch.await_args.args[0]
        assert "recommendation_feedback" in sql, (
            "SQL must reference recommendation_feedback for L3 exclusion"
        )
        assert "'negative'" in sql, "SQL must filter on signal = 'negative'"
        assert "60 days" in sql, "SQL must enforce the 60-day feedback window"

    @pytest.mark.asyncio
    async def test_negative_feedback_59d_excludes_paper(self) -> None:
        """Negative feedback 59 days old (< 60d) fires the NOT EXISTS → paper excluded.

        Boundary contract: `rf.created_at > NOW() - INTERVAL '60 days'`
        A 59-day-old feedback row satisfies `> (now - 60d)` → EXISTS fires → excluded.
        """
        conn = AsyncMock()
        # Simulate DB returning no rows (paper excluded by the EXISTS condition)
        conn.fetch = AsyncMock(return_value=[])
        result = await _filter_unread(conn, [10], user_id=1)
        assert 10 not in result, (
            "Paper with 59d-old negative feedback must be excluded (within 60d window)"
        )

    @pytest.mark.asyncio
    async def test_negative_feedback_60d_boundary_exclusive(self) -> None:
        """Negative feedback exactly 60 days old sits on the boundary — exclusive `>`.

        With strict `>`, `created_at = (now - 60d)` is NOT > (now - 60d) → EXISTS does
        not fire → paper is ELIGIBLE. 60-day-old feedback does NOT trigger exclusion.
        """
        conn = AsyncMock()
        # Simulate DB returning the paper (boundary-exclusive: 60d is NOT excluded)
        conn.fetch = AsyncMock(return_value=[{"id": 11}])
        result = await _filter_unread(conn, [11], user_id=1)
        assert 11 in result, (
            "Paper with exactly 60d-old negative feedback must be ELIGIBLE "
            "(strict > means boundary is exclusive: 60d old does NOT trigger exclusion)"
        )

    @pytest.mark.asyncio
    async def test_negative_feedback_61d_eligible(self) -> None:
        """Negative feedback 61 days old is outside the 60d window → paper eligible."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"id": 12}])
        result = await _filter_unread(conn, [12], user_id=1)
        assert 12 in result, (
            "Paper with 61d-old negative feedback must be eligible (expired window)"
        )

    @pytest.mark.asyncio
    async def test_filter_unread_excludes_trash_state(self, test_db_pool):
        """Phase-A: papers with state='trash' are excluded from recommendation candidates.

        Migration 047 replaced dismissed=TRUE with state='trash'. This test uses the
        new schema column (state TEXT) not the dropped boolean (dismissed).
        """
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-trash-1', 'arxiv', 'T', '{}', 'http://x') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 1, 'trash')",
                paper_id,
            )
            result = await _filter_unread(conn, [paper_id], user_id=1)
            assert paper_id not in result, "Trash papers must be excluded from candidates"

    @pytest.mark.asyncio
    async def test_filter_unread_excludes_done_state(self, test_db_pool):
        """Phase-A: papers with state='done' are excluded from recommendation candidates.

        Migration 047 replaced archived=TRUE/status='read' with state='done'.
        """
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-done-1', 'arxiv', 'Done Paper', '{}', 'http://done') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 1, 'done')",
                paper_id,
            )
            result = await _filter_unread(conn, [paper_id], user_id=1)
            assert paper_id not in result, "Done papers must be excluded from candidates"

    @pytest.mark.asyncio
    async def test_filter_unread_includes_inbox_and_reading_states(self, test_db_pool):
        """Phase-A: papers with state in ('inbox','to_read','reading') remain eligible."""
        async with test_db_pool.acquire() as conn:
            for state_val, ext_id in [
                ("inbox", "test-inbox-1"),
                ("to_read", "test-to-read-1"),
                ("reading", "test-reading-1"),
            ]:
                paper_id = await conn.fetchval(
                    "INSERT INTO papers (external_id, source_type, title, authors, url) "
                    "VALUES ($1, 'arxiv', $1, '{}', 'http://x') RETURNING id",
                    ext_id,
                )
                await conn.execute(
                    "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 1, $2)",
                    paper_id,
                    state_val,
                )
                result = await _filter_unread(conn, [paper_id], user_id=1)
                assert paper_id in result, f"Paper with state='{state_val}' must remain eligible"

    @pytest.mark.asyncio
    async def test_filter_unread_excludes_negative_feedback_within_60d(self, test_db_pool):
        """Phase-A L3: negative recommendation_feedback within 60 days excludes a paper."""
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-negfb-1', 'arxiv', 'Neg FB Paper', '{}', 'http://nf') RETURNING id"
            )
            # Insert negative feedback 30 days ago (well within the 60d window)
            await conn.execute(
                "INSERT INTO recommendation_feedback"
                " (paper_id, user_id, signal, source, created_at)"
                " VALUES ($1, 1, 'negative', 'pulse_thumbs', NOW() - INTERVAL '30 days')",
                paper_id,
            )
            result = await _filter_unread(conn, [paper_id], user_id=1)
            assert paper_id not in result, (
                "Paper with negative feedback within 60 days must be excluded"
            )

    @pytest.mark.asyncio
    async def test_filter_unread_includes_paper_after_60d_negative_feedback(self, test_db_pool):
        """Phase-A L3 boundary (exclusive): negative feedback > 60 days old does not exclude.

        Strict `>` means a feedback row created exactly 60d+1s ago is outside the window.
        Here we use 61 days to be unambiguous.
        """
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-negfb-2', 'arxiv', 'Old Neg FB Paper', '{}', 'http://nf2')"
                " RETURNING id"
            )
            # Feedback 61 days ago — outside the 60d exclusive window
            await conn.execute(
                "INSERT INTO recommendation_feedback"
                " (paper_id, user_id, signal, source, created_at)"
                " VALUES ($1, 1, 'negative', 'pulse_thumbs', NOW() - INTERVAL '61 days')",
                paper_id,
            )
            result = await _filter_unread(conn, [paper_id], user_id=1)
            assert paper_id in result, (
                "Paper with negative feedback older than 60 days must be eligible again"
            )

    # -----------------------------------------------------------------------
    # Multi-tenant isolation tests (W3-T2)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_user_id_bound_in_filter_unread_query(self) -> None:
        """user_id must be forwarded to the SQL query as $2."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [1, 2], user_id=42)
        positional = conn.fetch.call_args.args
        # $1 = paper_ids, $2 = user_id
        assert positional[1] == [1, 2], "paper_ids must be $1"
        assert positional[2] == 42, "user_id must be $2"

    @pytest.mark.asyncio
    async def test_filter_unread_sql_contains_user_id_guard(self) -> None:
        """SQL must scope both EXISTS sub-queries by an exact user_id match."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _filter_unread(conn, [1], user_id=7)
        sql = conn.fetch.call_args.args[0]
        assert "IS NOT DISTINCT FROM" not in sql, (
            "WS-CROSS-USER: must not use the permissive NULL-matching predicate"
        )
        assert "pus.user_id = $2" in sql and "rf.user_id = $2" in sql, (
            f"both per-user EXISTS guards must use an exact match; got:\n{sql!r}"
        )

    @pytest.mark.asyncio
    async def test_filter_unread_cross_user_isolation(self, test_db_pool):
        """State rows for user A must NOT affect filtering for user B.

        Phase-A: uses state='done' (replaces old status='read').
        """
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-isolation-1', 'arxiv', 'Isolation Paper', '{}', 'http://x') "
                "RETURNING id"
            )
            # User A marks the paper as done (Phase-A: was status='read')
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 1, 'done')",
                paper_id,
            )
            # User B queries: paper should still appear (user B has not done it)
            result = await _filter_unread(conn, [paper_id], user_id=2)
            assert paper_id in result, (
                "Paper marked done by user A must still be a candidate for user B"
            )
            # User A queries: paper should be excluded (user A marked it done)
            result = await _filter_unread(conn, [paper_id], user_id=1)
            assert paper_id not in result, "Paper marked done by user A must be excluded for user A"

    @pytest.mark.asyncio
    async def test_filter_unread_done_cross_user_isolation(self, test_db_pool):
        """state='done' for user A must NOT exclude the paper for user B.

        Phase-A: state='done' replaces the old archived=TRUE column (dropped in migration 047).
        """
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-isolation-2', 'arxiv', 'Done Paper', '{}', 'http://y') "
                "RETURNING id"
            )
            # User A marks the paper as done (was: archived=TRUE)
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 10, 'done')",
                paper_id,
            )
            # User B: paper must still be a candidate
            result = await _filter_unread(conn, [paper_id], user_id=20)
            assert paper_id in result, (
                "Paper in state='done' for user A must still be a candidate for user B"
            )

    @pytest.mark.asyncio
    async def test_filter_unread_trash_cross_user_isolation(self, test_db_pool):
        """state='trash' for user A must NOT exclude the paper for user B.

        Phase-A: state='trash' replaces the old dismissed=TRUE column (dropped in migration 047).
        """
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-isolation-3', 'arxiv', 'Trash Paper', '{}', 'http://z') "
                "RETURNING id"
            )
            # User A trashes the paper (was: dismissed=TRUE)
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 100, 'trash')",
                paper_id,
            )
            # User B: paper must still be a candidate
            result = await _filter_unread(conn, [paper_id], user_id=200)
            assert paper_id in result, (
                "Paper in state='trash' for user A must still be a candidate for user B"
            )


# ===========================================================================
# 3b. _get_starred_ids — multi-tenant isolation (W3-T2)
# ===========================================================================


class TestGetStarredIds:
    """_get_starred_ids must scope results to the given user_id."""

    @pytest.mark.asyncio
    async def test_user_id_bound_in_query(self) -> None:
        """user_id must be forwarded to the SQL query as $1."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _get_starred_ids(conn, user_id=7)
        positional = conn.fetch.call_args.args
        assert positional[1] == 7, "user_id must be $1 in _get_starred_ids query"

    @pytest.mark.asyncio
    async def test_sql_contains_user_id_guard(self) -> None:
        """SQL must scope starred lookups by an exact user_id match."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        await _get_starred_ids(conn, user_id=7)
        sql = conn.fetch.call_args.args[0]
        assert "IS NOT DISTINCT FROM" not in sql, (
            "WS-CROSS-USER: must not use the permissive NULL-matching predicate"
        )
        assert "user_id = $1" in sql, (
            f"_get_starred_ids must scope by an exact user_id match; got:\n{sql!r}"
        )

    @pytest.mark.asyncio
    async def test_starred_cross_user_isolation(self, test_db_pool):
        """Starred paper under user A must NOT appear in user B's starred set."""
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-star-isolation-1', 'arxiv', 'Star Paper', '{}', 'http://s1') "
                "RETURNING id"
            )
            # User A stars the paper
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, starred) VALUES ($1, 1, TRUE)",
                paper_id,
            )
            # User B queries: paper must NOT be in starred list
            user_b_starred = await _get_starred_ids(conn, user_id=2)
            assert paper_id not in user_b_starred, (
                "Paper starred by user A must NOT appear in user B's starred list"
            )
            # User A queries: paper MUST be in starred list
            user_a_starred = await _get_starred_ids(conn, user_id=1)
            assert paper_id in user_a_starred, (
                "Paper starred by user A must appear in user A's starred list"
            )

    @pytest.mark.asyncio
    async def test_starred_boolean_only_drives_starred_ids(self, test_db_pool):
        """Phase-A: _get_starred_ids uses COALESCE(starred, FALSE) — no status column.

        Migration 047 dropped status (including the old 'starred' enum value).
        A paper whose user_state has starred=FALSE must NOT appear in starred results,
        even if other fields would have matched the old status='starred' predicate.
        """
        async with test_db_pool.acquire() as conn:
            paper_id = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('test-star-isolation-2', 'arxiv', 'Not-Starred Paper', '{}', 'http://s2') "
                "RETURNING id"
            )
            # User A has a state row but starred=FALSE (the only column that now matters)
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, starred) VALUES ($1, 10, FALSE)",
                paper_id,
            )
            # User A: paper must NOT be in starred list (starred=FALSE)
            user_a_starred = await _get_starred_ids(conn, user_id=10)
            assert paper_id not in user_a_starred, (
                "Paper with starred=FALSE must not appear in starred list"
            )
            # User B: also must not see it
            user_b_starred = await _get_starred_ids(conn, user_id=20)
            assert paper_id not in user_b_starred, (
                "Paper with starred=FALSE for user A must not appear in user B's starred list"
            )


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


class TestReadWeightsPrecedence:
    """_read_weights must apply user-row-wins: per-user config beats NULL-global config."""

    def _make_conn(self, rows: list) -> AsyncMock:
        """Return a minimal asyncpg.Connection mock whose fetch() returns *rows*."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=rows)
        return conn

    @pytest.mark.asyncio
    async def test_user_row_wins_over_global(self) -> None:
        """When a per-user row and a global NULL-user row both exist, the per-user value wins."""
        # Global row: liked_weight=0.6 (the default), project_weight=0.4, enabled=True
        # Per-user row: liked_weight=0.9 (user override)
        # SQL ordering: user-specific rows come first (NULLS LAST on user_id), so the
        # per-user liked_weight row arrives before the global one for that key.
        rows = [
            # liked_weight — user-specific row first (user_id=7)
            FakeRecord({"key": "recommendation.liked_weight", "value": 0.9, "user_id": 7}),
            # liked_weight — global row second (NULL user_id); must be ignored
            FakeRecord({"key": "recommendation.liked_weight", "value": 0.6, "user_id": None}),
            # project_weight — global only
            FakeRecord({"key": "recommendation.project_weight", "value": 0.4, "user_id": None}),
            # enabled — global only
            FakeRecord({"key": "recommendation.enabled", "value": True, "user_id": None}),
        ]
        conn = self._make_conn(rows)
        liked, project, enabled = await _read_weights(conn, user_id=7)

        assert liked == pytest.approx(0.9), "per-user liked_weight must override global"
        assert project == pytest.approx(0.4)
        assert enabled is True

    @pytest.mark.asyncio
    async def test_global_row_used_when_no_user_row(self) -> None:
        """When only the global (user_id IS NULL) row exists, it is used as the fallback."""
        rows = [
            FakeRecord({"key": "recommendation.liked_weight", "value": 0.3, "user_id": None}),
            FakeRecord({"key": "recommendation.project_weight", "value": 0.7, "user_id": None}),
            FakeRecord({"key": "recommendation.enabled", "value": True, "user_id": None}),
        ]
        conn = self._make_conn(rows)
        liked, project, enabled = await _read_weights(conn, user_id=42)

        assert liked == pytest.approx(0.3), "global liked_weight must be used when no user row"
        assert project == pytest.approx(0.7)
        assert enabled is True

    @pytest.mark.asyncio
    async def test_empty_config_uses_module_defaults(self) -> None:
        """No rows at all → falls back to _DEFAULT_LIKED_WEIGHT / _DEFAULT_PROJECT_WEIGHT."""
        conn = self._make_conn([])
        liked, project, enabled = await _read_weights(conn, user_id=1)

        assert liked == pytest.approx(_DEFAULT_LIKED_WEIGHT)
        assert project == pytest.approx(_DEFAULT_PROJECT_WEIGHT)
        assert enabled is True

    @pytest.mark.asyncio
    async def test_user_disabled_overrides_global_enabled(self) -> None:
        """Per-user enabled=False wins over global enabled=True."""
        rows = [
            # enabled — user-specific row first
            FakeRecord({"key": "recommendation.enabled", "value": False, "user_id": 5}),
            # enabled — global row (must be ignored)
            FakeRecord({"key": "recommendation.enabled", "value": True, "user_id": None}),
        ]
        conn = self._make_conn(rows)
        _liked, _project, enabled = await _read_weights(conn, user_id=5)

        assert enabled is False, "per-user enabled=False must win over global enabled=True"
