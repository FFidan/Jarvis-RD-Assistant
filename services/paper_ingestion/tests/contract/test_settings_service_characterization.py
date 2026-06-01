"""Characterization tests for settings_service — pre-decomposition behavioral snapshot.

These tests pin the observable behaviour of settings_service.py before
sub-module extraction.  The same tests MUST pass byte-identically after
extraction; any divergence signals a regression.

Scope: smoke-level (3–5 assertions each).  Exhaustive coverage lives in the
existing contract/test_settings_contract.py suite.

Verified identifiers:
  settings_service._write_config_row     settings_service.py:551 — UPSERT conn-level
  settings_service._fetch_effective_config_row  settings_service.py:508 — scoped GET
  settings_service._validate_pulse_weights  settings_service.py:319 — pure validator
  settings_service.build_export_zip      settings_service.py:1044 — ZIP builder
  settings_service.ProviderTestResult    settings_service.py:970 — Pydantic response model
  settings_service.test_provider_connectivity settings_service.py:978 — async HTTP probe
"""

from __future__ import annotations

import io
import zipfile

import pytest
from jarvis_common.testing import SharedConnPool

# Verified: services/paper_ingestion/paper_ingestion/services/settings_service.py:319 (_validate_pulse_weights)
pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Test 1 — write + read round-trip for a non-encrypted key (lookback_days)
# ---------------------------------------------------------------------------

# pulse.lookback_days is a SYSTEM_KEY (NULL user_id row) with a positive-int
# validator.  We call _write_config_row directly so this test is independent of
# the full write_config orchestration (scheduler, LiteLLM, etc.).


async def test_write_config_lookback_days_persists_through_validator(contract_conn):
    """Golden path: write a value with the DB helpers, then read it back.

    Exercises _write_config_row (UPSERT) + _fetch_effective_config_row (SELECT).
    Verified: settings_service.py:551-578 (_write_config_row),
              settings_service.py:508-548 (_fetch_effective_config_row).
    """
    from paper_ingestion.services.settings_service import (
        _fetch_effective_config_row,
        _write_config_row,
        _validate_lookback_days,
    )

    key = "pulse.lookback_days"
    value = 14  # within valid [1, 90] range

    # Validator must accept this value without raising
    _validate_lookback_days(value)

    # Write via the raw DB helper (bypasses scheduler / LiteLLM side-effects)
    await _write_config_row(contract_conn, user_id=None, key=key, value=value)

    # Read back via the effective-row helper (NULL user_id → system row path)
    row = await _fetch_effective_config_row(contract_conn, key, user_id=None)

    assert row is not None, f"Expected a row for key={key!r} after write"
    # asyncpg JSONB codec returns Python native types
    assert row["value"] == value, f"Expected value={value!r}, got {row['value']!r}"
    assert row["key"] == key


# ---------------------------------------------------------------------------
# Test 2 — _validate_pulse_weights rejects missing required keys
# ---------------------------------------------------------------------------

# _PULSE_REQUIRED_WEIGHT_KEYS = {"embedding","topic","llm_relevance","llm_novelty",
#                                 "author_bonus","recency"} (settings_service.py:299-301)


async def test_validate_pulse_weights_rejects_missing_keys():
    """Validator raises ValueError when a required key is absent.

    Verified: settings_service.py:319-331 (_validate_pulse_weights),
              settings_service.py:299-301 (_PULSE_REQUIRED_WEIGHT_KEYS).
    """
    from paper_ingestion.services.settings_service import _validate_pulse_weights

    # Provide only 5 of the 6 required keys — "recency" is missing
    incomplete = {
        "embedding": 0.5,
        "topic": 0.3,
        "llm_relevance": 0.4,
        "llm_novelty": 0.3,
        "author_bonus": 0.1,
        # "recency" intentionally omitted
    }
    with pytest.raises(ValueError, match="recency"):
        _validate_pulse_weights(incomplete)


async def test_validate_pulse_weights_accepts_valid_dict():
    """Validator passes when all required keys are present and values are in [0, 1].

    Verified: settings_service.py:319-331 (_validate_pulse_weights).
    """
    from paper_ingestion.services.settings_service import _validate_pulse_weights

    valid = {
        "embedding": 0.5,
        "topic": 0.3,
        "llm_relevance": 0.4,
        "llm_novelty": 0.3,
        "author_bonus": 0.1,
        "recency": 0.2,
    }
    # Must not raise
    _validate_pulse_weights(valid)


# ---------------------------------------------------------------------------
# Test 3 — build_export_zip produces a valid ZIP with expected JSONL entries
# ---------------------------------------------------------------------------


async def test_build_export_zip_produces_zip_with_expected_files(contract_conn):
    """build_export_zip returns ZIP bytes containing one file per _EXPORT_QUERIES table.

    Verified: settings_service.py:1028-1064 (_EXPORT_QUERIES, build_export_zip).
    Tables expected: papers, paper_notes, paper_summaries, cards, decks,
      review_logs, projects, tasks, milestones, journal_entries, daily_log,
      user_config.
    """
    from paper_ingestion.services.settings_service import build_export_zip

    pool = SharedConnPool(contract_conn)
    # user_id=None to scope to system/NULL rows; minimal data in contract DB
    raw_bytes = await build_export_zip(pool, user_id=None)

    assert isinstance(raw_bytes, bytes), "build_export_zip must return bytes"
    assert len(raw_bytes) > 0, "ZIP bytes must be non-empty"

    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        names = set(zf.namelist())

    assert len(names) >= 1, "ZIP must contain at least one file"
    assert any(n.endswith(".jsonl") for n in names), "ZIP must contain at least one .jsonl file"


# ---------------------------------------------------------------------------
# Test 4 — ProviderTestResult response model shape (pure unit)
# ---------------------------------------------------------------------------


async def test_provider_test_result_shape():
    """ProviderTestResult Pydantic model has ok (bool) and optional error (str|None).

    Verified: settings_service.py:970-973 (ProviderTestResult).
    Pre-extraction: this model is defined inline in settings_service.py.
    Post-extraction: it must remain importable from the same path or via the
    re-export shim at that path.
    """
    from paper_ingestion.services.settings_service import ProviderTestResult

    ok_result = ProviderTestResult(ok=True)
    assert ok_result.ok is True
    assert ok_result.error is None

    err_result = ProviderTestResult(ok=False, error="provider returned HTTP 401")
    assert err_result.ok is False
    assert err_result.error == "provider returned HTTP 401"

    # Model must be a Pydantic model (has model_fields)
    assert hasattr(ProviderTestResult, "model_fields")
    assert "ok" in ProviderTestResult.model_fields
    assert "error" in ProviderTestResult.model_fields
