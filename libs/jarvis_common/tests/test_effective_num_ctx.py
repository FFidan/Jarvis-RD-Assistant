"""Unit tests for ``jarvis_common.db_helpers.effective_num_ctx``.

Pins the resolution order the prompt-input budget depends on:

1. Cloud model assigned → catalog context capped at the cost ceiling.
2. Delivered local context (the system ``llm.{role}_num_ctx`` row).
3. ``CoreSettings`` fallback (env/boot default), uncached.

Plus the failure mode: a DB read error falls back to CoreSettings and leaves
the cache empty (so a transient error cannot be cached and starve later reads).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common import db_helpers
from jarvis_common.db_helpers import (
    _CLOUD_INPUT_TOKEN_CEILING,
    effective_num_ctx,
    invalidate_effective_num_ctx_cache,
)


class _FakeRow(dict):
    """asyncpg-Record-like: indexable by column name."""


def _fake_db(rows: list[dict]) -> AsyncMock:
    db = AsyncMock()
    db.fetch = AsyncMock(return_value=[_FakeRow(r) for r in rows])
    return db


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_effective_num_ctx_cache()
    yield
    invalidate_effective_num_ctx_cache()


@pytest.mark.asyncio
async def test_cloud_model_capped_at_min_of_catalog_and_ceiling():
    """A cloud smart model resolves to min(catalog context, cost ceiling)."""
    db = _fake_db([{"key": "llm.smart_model", "value": "anthropic/claude-haiku-4-5"}])

    result = await effective_num_ctx(db, "smart")

    assert result <= 32768
    assert result == min(
        db_helpers._catalog_context_tokens("anthropic/claude-haiku-4-5")
        or _CLOUD_INPUT_TOKEN_CEILING,
        _CLOUD_INPUT_TOKEN_CEILING,
    )


@pytest.mark.asyncio
async def test_delivered_local_row_wins_over_fallback():
    """A delivered ``llm.{role}_num_ctx`` system row is returned verbatim."""
    db = _fake_db(
        [
            {"key": "llm.smart_model", "value": "ollama_chat/qwen3:8b"},
            {"key": "llm.smart_num_ctx", "value": 16384},
        ]
    )

    assert await effective_num_ctx(db, "smart") == 16384


@pytest.mark.asyncio
async def test_row_absent_falls_back_to_core_settings_uncached():
    """No delivered row → CoreSettings default, and that fallback is NOT cached."""
    from jarvis_common.settings import get_core_settings

    db = _fake_db([])  # no model row, no num_ctx row

    expected = get_core_settings().llm_smart_num_ctx
    assert await effective_num_ctx(db, "smart") == expected

    # Fallback must not be cached: a subsequent call re-reads the DB.
    db.fetch.reset_mock()
    assert await effective_num_ctx(db, "smart") == expected
    assert db.fetch.await_count == 1


@pytest.mark.asyncio
async def test_db_error_falls_back_and_cache_stays_empty():
    """A read failure returns the CoreSettings fallback and caches nothing."""
    from jarvis_common.settings import get_core_settings

    db = AsyncMock()
    db.fetch = AsyncMock(side_effect=RuntimeError("connection reset"))

    expected = get_core_settings().llm_fast_num_ctx
    assert await effective_num_ctx(db, "fast") == expected
    # The cache is empty, so a follow-up read hits the DB again (not a cached error).
    assert (
        db_helpers._effective_num_ctx_cache.get_cached("fast", __import__("time").monotonic())
        is None
    )
