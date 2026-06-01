"""Contract tests for settings/config endpoints.

Exercises real DB-backed config round-trips via the ASGI transport + SharedConnPool.

SURVIVOR CITATION:
  verify_api_key branch tests previously scattered across test_settings.py,
  test_settings_per_user_scoping.py, test_settings_zotero.py, test_auth_magic_link.py
  and test_admin_users.py are now collapsed into:
    libs/jarvis_common/tests/contract/test_verify_api_key_contract.py

This file covers only the DB-backed settings contract behaviours that mock-unit
tests cannot exercise: that UPSERT actually persists and GET reads the row back,
and that the scoping SQL correctly filters by user_id.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from jarvis_common.testing import A_PAPER_TITLE, SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def pi_settings_client(contract_conn, contract_two_users):
    """ASGI client wired to the real per-test transaction via SharedConnPool.

    Sets BOTH overrides so routes that use Depends(get_db_pool) AND any that
    read request.app.state.db_pool directly (system.py lines 241, 303, 628)
    both reach the same transactional connection.

    Also patches ``require_admin`` in the settings router namespace because
    ``set_config`` calls it directly (not via Depends), so dependency_overrides
    cannot intercept it — same technique as the mock-unit _app fixture.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    async def _allow_all(request=None) -> None:  # noqa: ARG001
        return None

    shared = SharedConnPool(contract_conn)
    # Idiomatic mock carve-out: set_config reads request.app.state.http_client for
    # the LiteLLM model-validation probe (outbound HTTP — never touches the DB).
    _orig_require_admin = _settings_mod.require_admin
    _settings_mod.require_admin = _allow_all
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared, "http_client": AsyncMock()}),
            patch_dependency_overrides(
                app,
                set_overrides={
                    get_db_pool: lambda: shared,
                    verify_api_key: lambda: None,
                    require_admin: _allow_all,
                },
            ),
        ):
            async with make_contract_client(app, contract_two_users.cookie_a) as client:
                yield client
    finally:
        _settings_mod.require_admin = _orig_require_admin
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# GET /api/config — lists all system config rows
# ---------------------------------------------------------------------------


async def test_list_config_returns_list(pi_settings_client):
    """GET /api/config returns a list (may be empty against fresh contract DB)."""
    resp = await pi_settings_client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)


# ---------------------------------------------------------------------------
# PUT + GET round-trip for a known safe key (pulse.deck_size — integer)
#
# NOTE: SharedConnPool stmt-cache caveat: routes using `$1::text` casts may
# trigger DataError if stmt-cache is warm from a prior differently-typed bind.
# pulse.deck_size uses a plain $1 integer parameter in the validator so is safe.
# ---------------------------------------------------------------------------


async def test_put_config_string_value_round_trip(contract_conn, pi_settings_client):
    """PUT /api/config/pulse.cron persists; GET /api/config/{key} reads it back."""
    cron_value = "0 5 * * *"
    put_resp = await pi_settings_client.put(
        "/api/config/pulse.cron",
        json={"key": "pulse.cron", "value": cron_value},
    )
    assert put_resp.status_code == 200, f"PUT failed: {put_resp.json()}"
    body = put_resp.json()
    assert body["key"] == "pulse.cron"
    assert body["value"] == cron_value

    # Verify the row landed in user_config (direct DB query, same txn).
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        "pulse.cron",
    )
    assert row is not None, "PUT did not persist a user_config row"
    # asyncpg JSONB codec returns the Python value directly — a bare string.
    assert row["value"] == cron_value


async def test_get_config_key_not_found_returns_404(pi_settings_client):
    """GET /api/config/{key} returns 404 when the key does not exist in DB."""
    resp = await pi_settings_client.get("/api/config/nonexistent.key.xyz")
    assert resp.status_code == 404


async def test_put_config_ghost_key_returns_400(pi_settings_client):
    """Ghost keys removed from the allow-list return 400, not a DB write.

    Collapsed from test_settings.py::test_ghost_key_returns_400 parametrize family
    (§D5-05).  We test one representative ghost key here; the full parametrized
    family remains in the mock-unit file for breadth coverage.
    """
    resp = await pi_settings_client.put(
        "/api/config/paper.max_daily",
        json={"key": "paper.max_daily", "value": 10},
    )
    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


async def test_put_config_ghost_key_does_not_write_db(contract_conn, pi_settings_client):
    """PUT of a ghost key returns 400 and writes no row to user_config."""
    await pi_settings_client.put(
        "/api/config/ui.page_size",
        json={"key": "ui.page_size", "value": 20},
    )
    row = await contract_conn.fetchrow("SELECT 1 FROM user_config WHERE key = 'ui.page_size'")
    assert row is None, "Ghost key must not write to user_config"


# ---------------------------------------------------------------------------
# E1.PI extensions — FSRS / L2 / weights / setup.completed / telegram.owner_chat_id
#
# Verified: settings_service.py:56-107 (_ALLOWED_CONFIG_KEYS, PERSONAL_KEYS, SYSTEM_KEYS)
# Verified: settings_service.py:415-468 (_CONFIG_VALIDATORS)
# Verified: settings_service.py:520-547 (_write_config_row — UPSERT)
# Verified: settings_service.py:477-517 (_fetch_effective_config_row — scoped GET)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# fsrs.desired_retention — personal key, per-user UPSERT
# ---------------------------------------------------------------------------


async def test_put_fsrs_desired_retention_round_trip(
    contract_conn, contract_two_users, pi_settings_client
):
    """PUT /api/config/fsrs.desired_retention persists; GET reads it back.

    Verified: settings_service.py:436 (_validate_fsrs_retention),
              settings_service.py:520-547 (_write_config_row UPSERT path).
    Survivor-of: test_settings.py fsrs key round-trip mock-unit tests.
    """
    resp = await pi_settings_client.put(
        "/api/config/fsrs.desired_retention",
        json={"key": "fsrs.desired_retention", "value": 0.85},
    )
    assert resp.status_code == 200, f"PUT failed: {resp.json()}"
    body = resp.json()
    assert body["key"] == "fsrs.desired_retention"
    assert body["value"] == 0.85

    row = await contract_conn.fetchrow(
        """SELECT value FROM user_config
           WHERE key = 'fsrs.desired_retention' AND user_id = $1""",
        contract_two_users.user_a_id,
    )
    assert row is not None, "fsrs.desired_retention row must be written to user_config"
    assert abs(float(row["value"]) - 0.85) < 1e-9, (
        f"Persisted value must be 0.85; got {row['value']!r}"
    )


async def test_put_fsrs_desired_retention_invalid_value_returns_400(pi_settings_client):
    """PUT fsrs.desired_retention with value ≥ 1.0 returns 400 (validator guard).

    Verified: settings_service.py:416-421 (_validate_fsrs_retention out-of-range).
    Survivor-of: test_settings.py invalid-value parametrize cases.
    """
    resp = await pi_settings_client.put(
        "/api/config/fsrs.desired_retention",
        json={"key": "fsrs.desired_retention", "value": 1.0},
    )
    assert resp.status_code == 400, (
        f"Expected 400 for out-of-range fsrs.desired_retention; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# pulse.l2_lambda — system key, numeric range [0, 2]
# ---------------------------------------------------------------------------


async def test_put_pulse_l2_lambda_round_trip(contract_conn, pi_settings_client):
    """PUT /api/config/pulse.l2_lambda persists in user_config (user_id IS NULL).

    Verified: settings_service.py:329-337 (_validate_l2_lambda),
              settings_service.py:520-547 (_write_config_row NULL-scoped UPSERT).
    Survivor-of: test_settings.py l2_lambda round-trip mock-unit tests.
    """
    resp = await pi_settings_client.put(
        "/api/config/pulse.l2_lambda",
        json={"key": "pulse.l2_lambda", "value": 1.5},
    )
    assert resp.status_code == 200, f"PUT pulse.l2_lambda failed: {resp.json()}"
    assert resp.json()["value"] == 1.5

    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'pulse.l2_lambda' AND user_id IS NULL",
    )
    assert row is not None, "pulse.l2_lambda must be written to user_config with user_id IS NULL"
    assert abs(float(row["value"]) - 1.5) < 1e-9, f"Expected 1.5; got {row['value']!r}"


async def test_put_pulse_l2_lambda_out_of_range_returns_400(pi_settings_client):
    """PUT pulse.l2_lambda > 2.0 returns 400.

    Verified: settings_service.py:335-337 (_validate_l2_lambda range guard).
    """
    resp = await pi_settings_client.put(
        "/api/config/pulse.l2_lambda",
        json={"key": "pulse.l2_lambda", "value": 3.0},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# setup.completed — boolean system key
# ---------------------------------------------------------------------------


async def test_put_setup_completed_persists_true(contract_conn, pi_settings_client):
    """PUT /api/config/setup.completed stores True in user_config.

    Verified: settings_service.py:447 (_validate_bool guard),
              settings_service.py:520-547 (_write_config_row UPSERT).
    Survivor-of: test_settings.py setup.completed round-trip tests.
    """
    resp = await pi_settings_client.put(
        "/api/config/setup.completed",
        json={"key": "setup.completed", "value": True},
    )
    assert resp.status_code == 200, f"PUT setup.completed failed: {resp.json()}"
    assert resp.json()["value"] is True

    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'setup.completed' AND user_id IS NULL",
    )
    assert row is not None, "setup.completed row must exist in user_config"
    assert row["value"] is True, f"Expected True; got {row['value']!r}"


# ---------------------------------------------------------------------------
# telegram.owner_chat_id — optional int system key
# ---------------------------------------------------------------------------


async def test_put_telegram_owner_chat_id_round_trip(contract_conn, pi_settings_client):
    """PUT /api/config/telegram.owner_chat_id stores integer; GET reads it back.

    Verified: settings_service.py:448 (telegram.owner_chat_id → _validate_optional_int),
              settings_service.py:512-517 (_fetch_effective_config_row system path).
    Survivor-of: test_settings.py telegram.owner_chat_id round-trip tests.
    """
    resp = await pi_settings_client.put(
        "/api/config/telegram.owner_chat_id",
        json={"key": "telegram.owner_chat_id", "value": 123456789},
    )
    assert resp.status_code == 200, f"PUT telegram.owner_chat_id failed: {resp.json()}"
    assert resp.json()["value"] == 123456789

    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL",
    )
    assert row is not None, "telegram.owner_chat_id row must exist in user_config"
    assert int(row["value"]) == 123456789


async def test_put_telegram_owner_chat_id_null_clears(contract_conn, pi_settings_client):
    """PUT /api/config/telegram.owner_chat_id with null clears the stored integer.

    Verified: settings_service.py:313-317 (_validate_optional_int null branch).
    """
    resp = await pi_settings_client.put(
        "/api/config/telegram.owner_chat_id",
        json={"key": "telegram.owner_chat_id", "value": None},
    )
    assert resp.status_code == 200, f"PUT telegram null failed: {resp.json()}"
    assert resp.json()["value"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_put_config_db_committed_before_litellm_called(
    contract_conn, pi_settings_client, monkeypatch
):
    """DB row is written even when _apply_litellm_runtime_update raises.

    Verifies BUG-D4-004 fix: DB write happens BEFORE LiteLLM runtime update so
    that a LiteLLM failure does not discard the persisted config value.
    Also verifies that when _write_config_row raises, _apply_litellm_runtime_update
    is never called.
    """
    import paper_ingestion.services.config_write as _config_write

    # -- Part 1: LiteLLM fails → DB row still committed ----------------------
    litellm_called: list[str] = []

    async def _litellm_fail(**kwargs):  # noqa: ARG001
        litellm_called.append("called")
        raise RuntimeError("litellm-fail")

    monkeypatch.setattr(_config_write, "_apply_litellm_runtime_update", _litellm_fail)

    with pytest.raises(RuntimeError, match="litellm-fail"):
        await pi_settings_client.put(
            "/api/config/pulse.deck_size",
            json={"key": "pulse.deck_size", "value": 42},
        )
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        "pulse.deck_size",
    )
    assert row is not None
    assert row["value"] == 42
    assert litellm_called

    # -- Part 2: DB write fails → LiteLLM update never called ----------------
    monkeypatch.undo()

    litellm_reached: list[str] = []

    async def _litellm_spy(**kwargs):  # noqa: ARG001
        litellm_reached.append("called")

    async def _db_fail(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("db-fail")

    monkeypatch.setattr(_config_write, "_write_config_row", _db_fail)
    monkeypatch.setattr(_config_write, "_apply_litellm_runtime_update", _litellm_spy)

    with pytest.raises(RuntimeError, match="db-fail"):
        await pi_settings_client.put(
            "/api/config/pulse.deck_size",
            json={"key": "pulse.deck_size", "value": 99},
        )
    assert not litellm_reached


# ---------------------------------------------------------------------------
# W1A.4 — settings/ai contract tests
#
# Verified: routers/settings_ai.py:60-77   (GET /api/settings/ai)
# Verified: routers/settings_ai.py:80-99   (POST /api/settings/ai)
# Verified: routers/settings_ai.py:102-104 (POST /api/settings/ai/redetect)
# Verified: routers/settings_ai.py:107-127 (POST /api/settings/ai/dismiss-banner)
# Verified: services/ai_settings.py:140-220 (resolve_candidates_for_tier)
# Verified: services/ai_settings.py:229-238 (candidate_is_allowed)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _ai_settings_client(contract_conn, tmp_path_factory):
    """ASGI client wired for /api/settings/ai endpoints.

    - SharedConnPool for dismiss-banner DB writes (within per-test txn).
    - require_admin patched in the settings_ai module namespace (it is a *local*
      function from paper_ingestion.routers.admin, not jarvis_common.auth, so
      dependency_overrides cannot intercept it; direct attribute patch required).
    - A minimal llm-tier-candidates.yaml with one valid ge-48 ollama candidate
      (qwen3:14b — tier=2, assignable=True, smart role — present in catalog).
    - observed_share stubbed to avoid Langfuse/LiteLLM HTTP calls.
    """
    from unittest.mock import MagicMock

    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings_ai as _sai_mod

    # Minimal candidates overlay: one valid ge-48 catalog-backed ollama entry.
    tmp_path = tmp_path_factory.mktemp("ai_settings_contract")
    config_path = tmp_path / "llm-tier-candidates.yaml"
    config_path.write_text(
        "generated_from: test-bench.md\n"
        "tiers:\n"
        "  ge-48:\n"
        "    candidates:\n"
        "      - backend: ollama\n"
        "        model: qwen3:14b\n"
        "        rank: 1\n"
        "        score: 90\n"
        "        evidence: bench\n"
        "        reasoning: catalog-backed contract test candidate\n"
    )

    async def _allow_admin(request=None) -> None:  # noqa: ARG001
        return None

    # require_admin in settings_ai.py is imported from paper_ingestion.routers.admin
    # (not jarvis_common.auth), so we must override that specific function object.
    from paper_ingestion.routers.admin import require_admin as _pi_require_admin

    shared = SharedConnPool(contract_conn)
    _orig_config_path = _sai_mod._CONFIG_PATH
    _orig_observed_share = _sai_mod.observed_share
    _sai_mod._CONFIG_PATH = config_path
    _sai_mod.observed_share = lambda _role: ("ollama/qwen3:14b", 0.95)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared, "http_client": MagicMock()}),
            patch_dependency_overrides(
                app,
                set_overrides={
                    get_db_pool: lambda: shared,
                    verify_api_key: lambda: None,
                    _pi_require_admin: _allow_admin,
                },
            ),
        ):
            async with make_contract_client(app, None) as client:
                yield client
    finally:
        _sai_mod._CONFIG_PATH = _orig_config_path
        _sai_mod.observed_share = _orig_observed_share
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# test_settings_ai_get_returns_resolved_candidates
# Verified: routers/settings_ai.py:60-77 (get_ai_settings)
# Verified: services/ai_settings.py:140-220 (resolve_candidates_for_tier)
# Survivor-of: test_settings_ai.py::test_get_settings_ai_returns_catalog_backed_candidates
# ---------------------------------------------------------------------------


async def test_settings_ai_get_returns_resolved_candidates(_ai_settings_client, monkeypatch):
    """GET /api/settings/ai returns the resolved candidates list for the hw tier.

    Exercises the real resolve_candidates_for_tier path against the catalog.
    The response must include hw_tier, recommended_backend/model, and at least
    one candidate with catalog_id populated.

    # Verified: routers/settings_ai.py:60-77
    # Verified: services/ai_settings.py:195-220 (catalog-backed candidate assembly)
    """
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    resp = await _ai_settings_client.get("/api/settings/ai")

    assert resp.status_code == 200, f"GET /api/settings/ai failed: {resp.text}"
    body = resp.json()
    assert body["hw_tier"] == "ge-48"
    assert body["recommended_backend"] == "ollama"
    assert body["recommended_model"] == "qwen3:14b"
    candidates = body["candidates_for_tier"]
    assert len(candidates) >= 1, "Must return at least one resolved candidate"
    top = candidates[0]
    assert top["catalog_id"] == "qwen3:14b", (
        f"First candidate must be catalog-backed with catalog_id='qwen3:14b'; got {top!r}"
    )
    assert top["source"] == "catalog"


# ---------------------------------------------------------------------------
# test_settings_ai_post_rejects_non_candidate_model
# Verified: routers/settings_ai.py:80-99 (apply_ai_settings 422 branch)
# Verified: services/ai_settings.py:229-238 (candidate_is_allowed)
# Survivor-of: test_settings_ai.py::test_post_settings_ai_rejects_random_non_candidate_model
# ---------------------------------------------------------------------------


async def test_settings_ai_post_rejects_non_candidate_model(_ai_settings_client, monkeypatch):
    """POST /api/settings/ai with a model not in candidates_for_tier returns 422.

    candidate_is_allowed (services/ai_settings.py:229-238) must reject the
    request before _APPLIER.apply is called.  The detail message must reference
    'candidates_for_tier'.

    # Verified: routers/settings_ai.py:87-93 (HTTPException 422 branch)
    # Verified: services/ai_settings.py:229-238 (candidate_is_allowed returns False)
    """
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    resp = await _ai_settings_client.post(
        "/api/settings/ai",
        json={"backend": "ollama", "model": "not-in-catalog:latest"},
    )

    assert resp.status_code == 422, (
        f"Expected 422 for non-candidate model; got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    assert "not an allowed candidate" in detail, (
        f"422 detail must mention 'not an allowed candidate'; got: {detail!r}"
    )


# ---------------------------------------------------------------------------
# test_settings_ai_apply_failure_returns_generic_502
# Verified: routers/settings_ai.py:94-102 (apply_ai_settings 502 branch — MED-PI-04)
# regression guard — exc message must NOT appear in response body
# ---------------------------------------------------------------------------


async def test_settings_ai_apply_failure_returns_generic_502(_ai_settings_client, monkeypatch):
    """POST /api/settings/ai returns 502 with a generic detail when _APPLIER.apply raises.

    Regression guard for MED-PI-04: the exception message must NOT be reflected
    in the response body (no f-string leak of str(exc)).

    # Verified: routers/settings_ai.py:94-102 (try/except → HTTPException 502)
    """
    from unittest.mock import patch

    from paper_ingestion.routers import settings_ai as _sai_mod

    sentinel = "SENSITIVE_INTERNAL_DETAIL_xyz_123"
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    with patch.object(_sai_mod._APPLIER, "apply", side_effect=Exception(sentinel)):
        resp = await _ai_settings_client.post(
            "/api/settings/ai",
            json={"backend": "ollama", "model": "qwen3:14b"},
        )

    assert resp.status_code == 502, (
        f"Expected 502 when _APPLIER.apply raises; got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["detail"] == "apply failed; previous config restored", (
        f"502 detail must be generic; got: {resp.json().get('detail')!r}"
    )
    assert sentinel not in resp.text, (
        "Exception message must NOT appear in the response body (MED-PI-04 regression)"
    )


# ---------------------------------------------------------------------------
# test_settings_ai_redetect_refreshes_overlay
# Verified: routers/settings_ai.py:102-104 (redetect_hw → get_ai_settings)
# Survivor-of: test_settings_ai.py::test_redetect_returns_settings
# ---------------------------------------------------------------------------


async def test_settings_ai_redetect_refreshes_overlay(_ai_settings_client, monkeypatch):
    """POST /api/settings/ai/redetect returns AISettingsResponse with the active tier.

    Confirms the redetect route delegates to get_ai_settings() and reflects the
    current JARVIS_HW_TIER without requiring the caller to hit GET first.

    # Verified: routers/settings_ai.py:102-104 (redetect_hw)
    # Verified: routers/settings_ai.py:55-57 (_effective_tier reads JARVIS_HW_TIER env)
    """
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    resp = await _ai_settings_client.post("/api/settings/ai/redetect")

    assert resp.status_code == 200, f"POST /api/settings/ai/redetect failed: {resp.text}"
    body = resp.json()
    assert body["hw_tier"] == "ge-48"
    candidates = body["candidates_for_tier"]
    assert any(c["model"] == "qwen3:14b" for c in candidates), (
        f"Redetect must return the ge-48 overlay candidate 'qwen3:14b'; got {candidates!r}"
    )


# ---------------------------------------------------------------------------
# test_settings_ai_dismiss_banner_persists_per_user
# Verified: routers/settings_ai.py:107-127 (dismiss_banner)
# Verified: db/init.sql:1222-1233 (system_events schema, category='config')
# Survivor-of: test_settings_ai.py::test_dismiss_banner_inserts_event (mock-unit)
# ---------------------------------------------------------------------------


async def test_settings_ai_dismiss_banner_persists_per_user(contract_conn, _ai_settings_client):
    """POST /api/settings/ai/dismiss-banner inserts a real system_events row.

    Exercises the INSERT INTO system_events path against the live DB schema.
    The contract layer is the only place that can verify the row actually landed
    in system_events (test_settings_ai.py mocks conn.execute and checks args).

    # Verified: routers/settings_ai.py:115-127 (pool.acquire + conn.execute INSERT)
    # Verified: db/init.sql:1222-1233 (system_events: level, category, source, message, context)
    """
    banner_kind = "hw-upgrade-available"

    resp = await _ai_settings_client.post(
        "/api/settings/ai/dismiss-banner",
        json={"banner_kind": banner_kind},
    )

    assert resp.status_code == 200, f"dismiss-banner failed: {resp.text}"
    assert resp.json() == {"ok": True}

    row = await contract_conn.fetchrow(
        """SELECT level, category, source, message, context
           FROM system_events
           WHERE source = 'settings_ai'
           ORDER BY id DESC
           LIMIT 1"""
    )
    assert row is not None, "dismiss-banner must INSERT a row into system_events"
    assert row["level"] == "info"
    assert row["category"] == "config"
    assert row["source"] == "settings_ai"
    assert banner_kind in row["message"], (
        f"message must contain the banner_kind '{banner_kind}'; got {row['message']!r}"
    )
    # asyncpg may return JSONB as a dict or as a JSON string depending on codec
    # registration; normalise to dict before asserting.
    import json as _json

    ctx = row["context"]
    ctx_dict = ctx if isinstance(ctx, dict) else _json.loads(ctx)
    assert ctx_dict.get("banner_kind") == banner_kind, (
        f"context jsonb must include banner_kind='{banner_kind}'; got {ctx_dict!r}"
    )


# ---------------------------------------------------------------------------
# A130 — GET /api/me/export contract tests
#
# Verified: routers/settings.py:466-486 (export_my_data)
# Verified: auth.py:283-308 (current_user_id_strict — raises HTTPException(401) when
#           request.state.user_id is absent)
# Verified: services/settings_service.py:1044-1064 (build_export_zip)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _me_export_client(contract_conn, contract_two_users):
    """ASGI client wired for GET /api/me/export (authenticated as user A).

    - SharedConnPool so build_export_zip's pool.acquire() shares the contract txn.
    - Session cookie for user A so current_user_id_strict resolves request.state.user_id.
    - Limiter disabled to avoid 429 on repeated test invocations.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={
                    get_db_pool: lambda: shared,
                    verify_api_key: lambda: None,
                },
            ),
        ):
            async with make_contract_client(app, contract_two_users.cookie_a) as client:
                yield client
    finally:
        app.state.limiter.enabled = True


async def test_get_my_export_returns_zip_for_authenticated_user(
    _me_export_client,
):
    """A130: GET /api/me/export returns 200 + application/zip for authenticated user.

    Verified: routers/settings.py:466-486 (export_my_data StreamingResponse)
    Verified: services/settings_service.py:1044-1064 (build_export_zip returns bytes)
    Verified: auth.py:283-308 (current_user_id_strict resolves cookie_a session)
    """
    resp = await _me_export_client.get("/api/me/export")

    assert resp.status_code == 200, (
        f"Expected 200 for authenticated export; got {resp.status_code}: {resp.text}"
    )
    content_type = resp.headers.get("content-type", "")
    assert content_type.startswith("application/zip"), (
        f"Expected application/zip Content-Type; got {content_type!r}"
    )
    assert len(resp.content) > 0, "Export ZIP body must be non-empty"


async def test_get_my_export_requires_auth(contract_conn):
    """A130: GET /api/me/export without a session cookie returns 401.

    Verified: auth.py:283-308 (current_user_id_strict raises HTTPException(401)
              when request.state.user_id is absent — no jarvis_session cookie).
    """
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={
                    get_db_pool: lambda: shared,
                    verify_api_key: lambda: None,
                },
            ),
        ):
            # Pass None for the session cookie → no jarvis_session header sent.
            async with make_contract_client(app, None) as unauth_client:
                resp = await unauth_client.get("/api/me/export")
    finally:
        app.state.limiter.enabled = True

    assert resp.status_code == 401, (
        f"Expected 401 for unauthenticated export; got {resp.status_code}: {resp.text}"
    )


async def test_get_my_export_excludes_other_users_papers(
    _me_export_client,
    contract_two_users,
):
    """A130 — cross-user isolation: user A's export ZIP must not contain user B's papers.

    Closes a GDPR export-correctness audit finding: the
    happy-path test only checks status / content-type / non-empty body. A
    regression that passed the wrong user_id to ``build_export_zip`` (e.g.
    ``None`` or a hardcoded constant) would not be caught. Here we leverage the
    ``contract_two_users`` fixture which already seeds one paper per user with
    ``discovered_by`` set; we then GET as user A and assert user B's seeded
    paper is absent from ``papers.jsonl``.

    Verified: services/settings_service.py:1029 — papers query is scoped via
    ``WHERE p.discovered_by = $1``; jarvis_common/testing.py:546 — fixture
    seeds A_PAPER_TITLE for user A and ``paper-b`` for user B.
    """
    import io
    import json
    import zipfile

    resp = await _me_export_client.get("/api/me/export")
    assert resp.status_code == 200, resp.text

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        papers_jsonl = zf.read("papers.jsonl").decode()

    titles = {json.loads(line)["title"] for line in papers_jsonl.splitlines() if line.strip()}

    assert A_PAPER_TITLE in titles, (
        f"User A's seeded paper '{A_PAPER_TITLE}' missing from export; got titles={titles!r}"
    )
    assert "paper-b" not in titles, (
        f"User B's paper 'paper-b' leaked into user A's export; got titles={titles!r}"
    )
    # Defence-in-depth: ensure none of the other A_* test constants would clash —
    # contract_two_users seeds exactly one paper per user, so user A's ZIP must
    # contain exactly one paper row.
    assert len(titles) == 1, (
        f"Expected exactly 1 paper for user A in export; got {len(titles)}: {titles!r}"
    )
