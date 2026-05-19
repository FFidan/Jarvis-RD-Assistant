"""NEW-M10 — Regex whitelist validation for dynamic_update extra_sets.

Verifies that the _EXTRA_SET_RE whitelist accepts only the three safe forms
(col = NOW(), col = NULL, col = $N) and rejects arbitrary SQL fragments.

DOM-E-04 — assert_paper_ownership NULL semantics in multi-tenant mode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common.db_helpers import assert_paper_ownership, dynamic_update

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn_returning(record: dict) -> AsyncMock:
    """Return a mock asyncpg connection whose fetchrow resolves to *record*."""
    conn = AsyncMock()
    conn.fetchrow.return_value = record
    return conn


_BASE_RECORD = {"id": 1, "name": "x", "updated_at": None, "completed_at": None, "flag": None}

_CALL_KWARGS: dict = dict(
    table="topics",
    record_id=1,
    updates={"name": "x"},
    allowed_columns=frozenset({"name"}),
)


# ---------------------------------------------------------------------------
# Positive cases — all accepted by the whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_update_accepts_now():
    """'updated_at = NOW()' must be accepted without error."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(conn, extra_sets=["updated_at = NOW()"], **_CALL_KWARGS)
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "updated_at = NOW()" in sql


@pytest.mark.asyncio
async def test_dynamic_update_accepts_null():
    """'flag = NULL' must be accepted without error."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(conn, extra_sets=["flag = NULL"], **_CALL_KWARGS)
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "flag = NULL" in sql


@pytest.mark.asyncio
async def test_dynamic_update_accepts_placeholder():
    """'col = $5' (positional placeholder) must be accepted without error."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(conn, extra_sets=["flag = $5"], **_CALL_KWARGS)
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "flag = $5" in sql


@pytest.mark.asyncio
async def test_dynamic_update_accepts_multiple_valid_fragments():
    """Multiple valid fragments in extra_sets should all be accepted."""
    conn = _conn_returning(_BASE_RECORD)
    await dynamic_update(
        conn,
        extra_sets=["updated_at = NOW()", "completed_at = NULL"],
        **_CALL_KWARGS,
    )
    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    assert "updated_at = NOW()" in sql
    assert "completed_at = NULL" in sql


# ---------------------------------------------------------------------------
# Negative cases — all rejected by the whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_update_rejects_literal():
    """A string literal value like 'col = \\'literal_string\\'' must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = 'literal_string'"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_subquery():
    """A subquery fragment 'col = (SELECT 1)' must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = (SELECT 1)"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_function_call():
    """An arbitrary function call 'col = some_func()' must raise ValueError (only NOW() is whitelisted)."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = some_func()"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_semicolon_injection():
    """SQL injection via semicolon must be rejected."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = NOW(); DROP TABLE papers --"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_subquery_assignment():
    """'col = (SELECT password FROM secrets)' must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = (SELECT password FROM secrets)"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_rejects_numeric_literal():
    """'col = 42' (bare numeric) must raise ValueError."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match="disallowed fragments"):
        await dynamic_update(
            conn,
            extra_sets=["col = 42"],
            **_CALL_KWARGS,
        )
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_update_error_mentions_bad_fragment():
    """The ValueError message must name the offending fragment for easy debugging."""
    conn = AsyncMock()
    with pytest.raises(ValueError, match=r"col = 'bad'"):
        await dynamic_update(
            conn,
            extra_sets=["col = 'bad'"],
            **_CALL_KWARGS,
        )


# ---------------------------------------------------------------------------
# DOM-E-04 — assert_paper_ownership NULL discovered_by semantics (D4 decided)
# ---------------------------------------------------------------------------


def _ownership_conn(*, discovered_by: int | None, in_library: int | None = None) -> AsyncMock:
    """Build a mock asyncpg connection for assert_paper_ownership tests."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"discovered_by": discovered_by}
    conn.fetchval.return_value = in_library
    return conn


@pytest.mark.asyncio
async def test_assert_ownership_null_discovered_by_is_free_pass():
    """DOM-E-04 (D4): discovered_by IS NULL → globally accessible, no library row needed.

    Canonical-corpus papers (discovered_by IS NULL) are shared by design.
    The prior multitenant_enabled knob was removed; the free pass is permanent.
    Full contract is pinned in test_ownership_canonical_invariant.py.
    """
    conn = _ownership_conn(discovered_by=None)
    # Should return without raising.
    await assert_paper_ownership(conn, paper_id=1, user_id=42)
    # fetchval (library check) must NOT have been called — early return.
    conn.fetchval.assert_not_awaited()


# ---------------------------------------------------------------------------
# JC-005 — validated_model_with_reason surfaces fallback reason
# (migrated from test_sprint4_1a.py)
# ---------------------------------------------------------------------------


class TestValidatedModelWithReason:
    def test_valid_alias_returns_none_reason(self) -> None:
        """Valid LiteLLM aliases should return (alias, None)."""
        from jarvis_common.db_helpers import validated_model_with_reason

        alias, reason = validated_model_with_reason("smart")
        assert alias == "smart"
        assert reason is None

    def test_fast_alias_returns_none_reason(self) -> None:
        from jarvis_common.db_helpers import validated_model_with_reason

        alias, reason = validated_model_with_reason("fast")
        assert alias == "fast"
        assert reason is None

    def test_embed_alias_returns_none_reason(self) -> None:
        from jarvis_common.db_helpers import validated_model_with_reason

        alias, reason = validated_model_with_reason("embed")
        assert alias == "embed"
        assert reason is None

    def test_invalid_model_returns_smart_with_reason(self) -> None:
        """Invalid model name should fall back to 'smart' and report why."""
        from jarvis_common.db_helpers import validated_model_with_reason

        alias, reason = validated_model_with_reason("mistral-nemo:latest")
        assert alias == "smart"
        assert reason is not None
        assert "mistral-nemo:latest" in reason

    def test_validated_model_returns_fallback_reason_on_invalid_input(self) -> None:
        """The original validated_model() still returns a plain str (no regression)."""
        from jarvis_common.db_helpers import validated_model, validated_model_with_reason

        # Plain function still returns str
        result = validated_model("some-unknown-model")
        assert result == "smart"
        assert isinstance(result, str)

        # Sibling returns tuple with non-None reason
        alias, reason = validated_model_with_reason("some-unknown-model")
        assert alias == "smart"
        assert reason is not None and len(reason) > 0

    def test_reason_contains_original_model_name(self) -> None:
        """Fallback reason message must contain the original model name."""
        from jarvis_common.db_helpers import validated_model_with_reason

        alias, reason = validated_model_with_reason("gpt-4-turbo")
        assert alias == "smart"
        assert reason is not None
        assert "gpt-4-turbo" in reason

    def test_exported_from_jarvis_common_top_level(self) -> None:
        """validated_model_with_reason must be importable from jarvis_common directly."""
        from jarvis_common import validated_model_with_reason  # noqa: F401
