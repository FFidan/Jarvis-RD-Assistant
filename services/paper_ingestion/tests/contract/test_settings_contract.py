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
import asyncpg
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


async def test_list_config_user_row_wins_over_null_row(
    contract_conn, contract_two_users, pi_settings_client
):
    """When both a user-specific row and a NULL-user row exist for the same key,
    GET /api/config (non-admin browser user) returns the USER row, not the global.

    Locks in the DISTINCT ON (key) ... ORDER BY key, user_id IS NULL precedence:
    in Postgres `false < true`, so the user row (user_id IS NULL → false) sorts
    first and DISTINCT ON keeps it. (Audit fix #4 was REFUTED — no code change.)
    """
    key = "recommendation.liked_weight"  # PERSONAL_KEY, non-secret, raw value
    user_a = contract_two_users.user_a_id

    # Global (NULL-user) row.
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES (NULL, $1, $2::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""",
        key,
        "0.1",
    )
    # User-specific row for the calling user (cookie_a → user_a).
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES ($1, $2, $3::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""",
        user_a,
        key,
        "0.9",
    )

    resp = await pi_settings_client.get("/api/config")
    assert resp.status_code == 200
    by_key = {e["key"]: e["value"] for e in resp.json()}
    # asyncpg's jsonb codec stores the Python str inputs as JSON strings.
    assert by_key.get(key) == "0.9", (
        f"User row (0.9) must shadow the global row (0.1) for {key!r}; got {by_key.get(key)!r}"
    )


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


async def test_put_encrypted_key_masked_sentinel_does_not_clobber_secret(
    contract_conn, pi_settings_client, monkeypatch
):
    """Re-submitting the masked display value of an encrypted key must NOT overwrite the stored secret.

    Verified: config_write.py:388 (encrypt_secret(str(value))), config_db.py:122
    (mask_secret on read), crypto.py:236 ('****'+last4 sentinel).
    """
    from cryptography.fernet import Fernet
    from jarvis_common.crypto import decrypt_secret, mask_secret, refresh_fernet_cache

    monkeypatch.setenv("JARVIS_CONFIG_KEY", Fernet.generate_key().decode())
    refresh_fernet_cache()

    real_secret = "sk-openai-REAL-secret-9876"
    put1 = await pi_settings_client.put(
        "/api/config/llm.openai.api_key",
        json={"key": "llm.openai.api_key", "value": real_secret},
    )
    assert put1.status_code == 200, f"initial PUT failed: {put1.text[:300]}"

    stored = await contract_conn.fetchrow(
        "SELECT encrypted_value FROM user_config WHERE key = 'llm.openai.api_key' AND user_id IS NULL"
    )
    assert stored is not None and stored["encrypted_value"] is not None
    assert decrypt_secret(stored["encrypted_value"].decode("ascii")) == real_secret

    # Resubmit the masked sentinel (what GET returns) — must be a no-op, not a clobber.
    masked = mask_secret(real_secret)
    assert masked.startswith("****")
    put2 = await pi_settings_client.put(
        "/api/config/llm.openai.api_key",
        json={"key": "llm.openai.api_key", "value": masked},
    )
    assert put2.status_code == 200, f"masked PUT failed: {put2.text[:300]}"

    after = await contract_conn.fetchrow(
        "SELECT encrypted_value FROM user_config WHERE key = 'llm.openai.api_key' AND user_id IS NULL"
    )
    assert decrypt_secret(after["encrypted_value"].decode("ascii")) == real_secret, (
        "Resubmitting the masked sentinel must NOT overwrite the stored secret"
    )
    refresh_fernet_cache()


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


async def _seed_zotero_library_state(contract_conn, contract_two_users) -> int:
    """Seed independent remote Zotero caches for both contract users."""
    user_a = contract_two_users.user_a_id
    user_b = contract_two_users.user_b_id
    paper_b = await contract_conn.fetchval(
        "SELECT paper_id FROM user_library WHERE user_id = $1",
        user_b,
    )
    await contract_conn.executemany(
        """INSERT INTO user_config (user_id, key, value)
           VALUES ($1, $2, $3::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""",
        [
            (user_a, "zotero.user_id", "library-a"),
            (user_a, "zotero.last_library_version", 17),
            (user_b, "zotero.user_id", "library-b"),
            (user_b, "zotero.last_library_version", 23),
        ],
    )
    await contract_conn.execute(
        "UPDATE projects SET zotero_collection_key = 'COLLECTION-A' WHERE user_id = $1",
        user_a,
    )
    await contract_conn.execute(
        "UPDATE projects SET zotero_collection_key = 'COLLECTION-B' WHERE user_id = $1",
        user_b,
    )
    await contract_conn.executemany(
        """INSERT INTO paper_user_zotero_links
               (paper_id, user_id, zotero_item_key, zotero_citation_key,
                zotero_attachment_key, zotero_last_pushed_at,
                analysis_enqueued_at, analysis_enqueue_attempts)
           VALUES ($1, $2, $3, $4, $5, NOW(), NOW(), $6)
           ON CONFLICT (paper_id, user_id) DO UPDATE SET
               zotero_item_key = EXCLUDED.zotero_item_key,
               zotero_citation_key = EXCLUDED.zotero_citation_key,
               zotero_attachment_key = EXCLUDED.zotero_attachment_key,
               zotero_last_pushed_at = EXCLUDED.zotero_last_pushed_at,
               analysis_enqueued_at = EXCLUDED.analysis_enqueued_at,
               analysis_enqueue_attempts = EXCLUDED.analysis_enqueue_attempts""",
        [
            (contract_two_users.paper_id_a, user_a, "ITEM-A", "CITE-A", "ATTACH-A", 2),
            (int(paper_b), user_b, "ITEM-B", "CITE-B", "ATTACH-B", 3),
        ],
    )
    return int(paper_b)


async def test_zotero_library_change_clears_only_callers_remote_cache(
    contract_conn, contract_two_users, pi_settings_client
):
    """Changing library identity preserves local history and the other user's cache."""
    paper_b = await _seed_zotero_library_state(contract_conn, contract_two_users)
    user_a = contract_two_users.user_a_id
    user_b = contract_two_users.user_b_id

    response = await pi_settings_client.put(
        "/api/config/zotero.user_id",
        json={"key": "zotero.user_id", "value": "library-a-new"},
    )

    assert response.status_code == 200, response.text
    link_a = await contract_conn.fetchrow(
        """SELECT zotero_item_key, zotero_citation_key, zotero_attachment_key,
                  zotero_last_pushed_at, analysis_enqueued_at, analysis_enqueue_attempts
             FROM paper_user_zotero_links
            WHERE paper_id = $1 AND user_id = $2""",
        contract_two_users.paper_id_a,
        user_a,
    )
    assert link_a is not None
    assert tuple(link_a)[:4] == (None, None, None, None)
    assert link_a["analysis_enqueued_at"] is not None
    assert link_a["analysis_enqueue_attempts"] == 2
    link_b = await contract_conn.fetchrow(
        """SELECT zotero_item_key, zotero_citation_key, zotero_attachment_key,
                  analysis_enqueue_attempts
             FROM paper_user_zotero_links
            WHERE paper_id = $1 AND user_id = $2""",
        paper_b,
        user_b,
    )
    assert tuple(link_b) == ("ITEM-B", "CITE-B", "ATTACH-B", 3)
    projects = await contract_conn.fetch(
        "SELECT user_id, zotero_collection_key FROM projects ORDER BY user_id"
    )
    collections = {row["user_id"]: row["zotero_collection_key"] for row in projects}
    assert collections[user_a] is None
    assert collections[user_b] == "COLLECTION-B"
    cursors = await contract_conn.fetch(
        "SELECT user_id, value FROM user_config WHERE key = 'zotero.last_library_version'"
    )
    assert {row["user_id"]: row["value"] for row in cursors} == {user_b: 23}


async def test_zotero_cache_survives_identical_scope_and_unrelated_writes(
    contract_conn, contract_two_users, pi_settings_client
):
    """Only a material library identity change invalidates remote linkage."""
    await _seed_zotero_library_state(contract_conn, contract_two_users)
    for key, value in (
        ("zotero.user_id", "library-a"),
        ("zotero.auto_push_on_star", True),
    ):
        response = await pi_settings_client.put(
            f"/api/config/{key}",
            json={"key": key, "value": value},
        )
        assert response.status_code == 200, response.text

    link = await contract_conn.fetchrow(
        """SELECT zotero_item_key, zotero_citation_key, zotero_attachment_key,
                  analysis_enqueued_at, analysis_enqueue_attempts
             FROM paper_user_zotero_links
            WHERE paper_id = $1 AND user_id = $2""",
        contract_two_users.paper_id_a,
        contract_two_users.user_a_id,
    )
    assert tuple(link)[:3] == ("ITEM-A", "CITE-A", "ATTACH-A")
    assert link["analysis_enqueued_at"] is not None
    assert link["analysis_enqueue_attempts"] == 2
    assert (
        await contract_conn.fetchval(
            "SELECT zotero_collection_key FROM projects WHERE id = $1",
            contract_two_users.project_id_a,
        )
        == "COLLECTION-A"
    )


async def test_zotero_library_change_rolls_back_config_and_cache_together(
    contract_conn, contract_two_users, pi_settings_client
):
    """A cache-reset failure cannot commit a mismatched library identity."""
    await _seed_zotero_library_state(contract_conn, contract_two_users)
    user_a = contract_two_users.user_a_id
    await contract_conn.execute(
        """CREATE FUNCTION fail_zotero_collection_reset() RETURNS trigger
           LANGUAGE plpgsql AS $$
           BEGIN
               RAISE EXCEPTION 'forced Zotero collection reset failure';
           END;
           $$"""
    )
    await contract_conn.execute(
        """CREATE TRIGGER fail_zotero_collection_reset
           BEFORE UPDATE OF zotero_collection_key ON projects
           FOR EACH ROW
           EXECUTE FUNCTION fail_zotero_collection_reset()"""
    )

    with pytest.raises(asyncpg.RaiseError, match="forced Zotero collection reset failure"):
        await pi_settings_client.put(
            "/api/config/zotero.user_id",
            json={"key": "zotero.user_id", "value": "library-a-new"},
        )

    assert (
        await contract_conn.fetchval(
            "SELECT value FROM user_config WHERE user_id = $1 AND key = 'zotero.user_id'",
            user_a,
        )
        == "library-a"
    )
    assert (
        await contract_conn.fetchval(
            """SELECT zotero_item_key FROM paper_user_zotero_links
               WHERE paper_id = $1 AND user_id = $2""",
            contract_two_users.paper_id_a,
            user_a,
        )
        == "ITEM-A"
    )
    assert (
        await contract_conn.fetchval(
            "SELECT value FROM user_config WHERE user_id = $1 AND key = 'zotero.last_library_version'",
            user_a,
        )
        == 17
    )


async def test_owner_user_id_not_admin_writable(contract_conn, pi_settings_client):
    """The owner.user_id system row is writable ONLY by create_first_admin.

    It is absent from the config allow-list, so PUT /api/config/owner.user_id is
    rejected (400) and never reaches the DB. Pins the invariant that the owner
    record cannot be reassigned through the admin config surface.
    """
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY
    from paper_ingestion.services.config_metadata import _ALLOWED_CONFIG_KEYS

    assert OWNER_USER_ID_CONFIG_KEY not in _ALLOWED_CONFIG_KEYS

    resp = await pi_settings_client.put(
        f"/api/config/{OWNER_USER_ID_CONFIG_KEY}",
        json={"key": OWNER_USER_ID_CONFIG_KEY, "value": 999},
    )
    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]

    row = await contract_conn.fetchrow(
        "SELECT 1 FROM user_config WHERE key = $1",
        OWNER_USER_ID_CONFIG_KEY,
    )
    assert row is None, "owner.user_id must not be writable via PUT /api/config"


# ---------------------------------------------------------------------------
# E1.PI extensions — FSRS / L2 / weights / setup.completed / telegram.owner_chat_id
#
# Verified: config_metadata.py (_ALLOWED_CONFIG_KEYS, PERSONAL_KEYS, SYSTEM_KEYS)
# Verified: config_validators.py (_CONFIG_VALIDATORS)
# Verified: config_db.py (_write_config_row — UPSERT)
# Verified: config_db.py (_fetch_effective_config_row — scoped GET)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# fsrs.desired_retention — personal key, per-user UPSERT
# ---------------------------------------------------------------------------


async def test_put_fsrs_desired_retention_round_trip(
    contract_conn, contract_two_users, pi_settings_client
):
    """PUT /api/config/fsrs.desired_retention persists; GET reads it back.

    Verified: config_validators.py (_validate_fsrs_retention),
              config_db.py (_write_config_row UPSERT path).
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

    Verified: config_validators.py (_validate_fsrs_retention out-of-range).
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

    Verified: config_validators.py (_validate_l2_lambda),
              config_db.py (_write_config_row NULL-scoped UPSERT).
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

    Verified: config_validators.py (_validate_l2_lambda range guard).
    """
    resp = await pi_settings_client.put(
        "/api/config/pulse.l2_lambda",
        json={"key": "pulse.l2_lambda", "value": 3.0},
    )
    assert resp.status_code == 400


async def test_put_onboarding_dismissed_round_trip_is_personal(
    contract_conn, contract_two_users, pi_settings_client
):
    """The tour dismissal written by the Web UI must be readable for that user."""
    resp = await pi_settings_client.put(
        "/api/config/onboarding.dismissed",
        json={"key": "onboarding.dismissed", "value": True},
    )
    assert resp.status_code == 200, f"PUT failed: {resp.json()}"

    row = await contract_conn.fetchrow(
        """SELECT value FROM user_config
           WHERE key = 'onboarding.dismissed' AND user_id = $1""",
        contract_two_users.user_a_id,
    )
    assert row is not None
    assert row["value"] is True

    fetched = await pi_settings_client.get("/api/config/onboarding.dismissed")
    assert fetched.status_code == 200
    assert fetched.json() == {"key": "onboarding.dismissed", "value": True}


# ---------------------------------------------------------------------------
# setup.completed — boolean system key
# ---------------------------------------------------------------------------


async def test_put_setup_completed_persists_true(contract_conn, pi_settings_client):
    """PUT /api/config/setup.completed stores True in user_config.

    Verified: config_validators.py (_validate_bool guard),
              config_db.py (_write_config_row UPSERT).
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


async def test_put_api_key_login_enabled_persists_and_invalidates_cache(
    contract_conn, pi_settings_client
):
    """PUT /api/config/auth.api_key_login_enabled persists the system row + clears the cache.

    The DB override is read by jarvis_common.auth.api_key_login_enabled through a
    process cache; the settings write must invalidate it so the next mint sees
    the new value. Verified: config_write.py invalidate_api_key_login_cache hook.
    """
    from jarvis_common import auth as _auth_mod
    from jarvis_common.auth import API_KEY_LOGIN_CONFIG_KEY

    # Prime the cache to a stale False so we can prove invalidation runs.
    _auth_mod._api_key_login_db_override = False

    resp = await pi_settings_client.put(
        f"/api/config/{API_KEY_LOGIN_CONFIG_KEY}",
        json={"key": API_KEY_LOGIN_CONFIG_KEY, "value": True},
    )
    assert resp.status_code == 200, f"PUT {API_KEY_LOGIN_CONFIG_KEY} failed: {resp.json()}"
    assert resp.json()["value"] is True

    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        API_KEY_LOGIN_CONFIG_KEY,
    )
    assert row is not None, f"{API_KEY_LOGIN_CONFIG_KEY} row must exist in user_config"
    assert row["value"] is True, f"Expected True; got {row['value']!r}"
    assert _auth_mod._api_key_login_db_override is None, (
        "settings write must invalidate the API-key-login cache"
    )


async def test_put_api_key_login_enabled_rejects_non_bool(pi_settings_client):
    """A non-boolean value for the API-key-login toggle is rejected with 400."""
    from jarvis_common.auth import API_KEY_LOGIN_CONFIG_KEY

    resp = await pi_settings_client.put(
        f"/api/config/{API_KEY_LOGIN_CONFIG_KEY}",
        json={"key": API_KEY_LOGIN_CONFIG_KEY, "value": "yes"},
    )
    assert resp.status_code == 400, f"Expected 400 for non-bool, got {resp.status_code}"


# ---------------------------------------------------------------------------
# telegram.owner_chat_id — optional int system key
# ---------------------------------------------------------------------------


async def test_put_telegram_owner_chat_id_round_trip(contract_conn, pi_settings_client):
    """PUT /api/config/telegram.owner_chat_id stores integer; GET reads it back.

    Verified: config_validators.py (telegram.owner_chat_id → _validate_optional_int),
              config_db.py (_fetch_effective_config_row system path).
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

    Verified: config_validators.py (_validate_optional_int null branch).
    """
    resp = await pi_settings_client.put(
        "/api/config/telegram.owner_chat_id",
        json={"key": "telegram.owner_chat_id", "value": None},
    )
    assert resp.status_code == 200, f"PUT telegram null failed: {resp.json()}"
    assert resp.json()["value"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_put_config_litellm_delivery_ordering(contract_conn, pi_settings_client, monkeypatch):
    """Fail-closed delivery contract for LiteLLM runtime keys (CRIT-1).

    Part 1: a real (non-"No DB Connected") delivery failure returns 400 and
    writes NO config row, so the UI snap-back is truthful.
    Part 1b: the stock-compose "No DB Connected" failure commits the row,
    returns HTTP 200, and records the role in llm.delivery_pending so
    GET /api/system/models surfaces delivery="pending_restart" — never a
    silent phantom "applied".
    Part 2: delivery fires first; when the subsequent DB write fails the PUT
    raises (no row committed) and one reconciler pass re-delivers the stored
    (old) model back to LiteLLM.
    """
    from fastapi import HTTPException

    import paper_ingestion.services.config_write as _config_write

    # -- Part 1: delivery fails (non-No-DB) → 400 + row NOT written ----------
    litellm_called: list[str] = []

    async def _litellm_fail(**kwargs):  # noqa: ARG001
        litellm_called.append("called")
        raise HTTPException(status_code=400, detail="litellm-fail")

    monkeypatch.setattr(_config_write, "_apply_litellm_runtime_update", _litellm_fail)

    resp = await pi_settings_client.put(
        "/api/config/llm.contract-host.smart_num_ctx",
        json={"key": "llm.contract-host.smart_num_ctx", "value": 4096},
    )
    assert resp.status_code == 400
    assert "litellm-fail" in resp.json()["detail"]
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        "llm.contract-host.smart_num_ctx",
    )
    assert row is None, "fail-closed: a delivery failure must not commit the row"
    assert litellm_called

    # -- Part 1b: "No DB Connected" → 200 + row committed + role pending -----
    async def _litellm_no_db(**kwargs):  # noqa: ARG001
        raise HTTPException(
            status_code=400,
            detail=(
                "LiteLLM /model/new failed for alias 'smart': "
                'HTTP 500 {"error": "No DB Connected"}'
            ),
        )

    monkeypatch.setattr(_config_write, "_apply_litellm_runtime_update", _litellm_no_db)

    resp = await pi_settings_client.put(
        "/api/config/llm.contract-host.smart_num_ctx",
        json={"key": "llm.contract-host.smart_num_ctx", "value": 8192},
    )
    assert resp.status_code == 200, f"No-DB carve-out must return 200: {resp.json()}"
    assert resp.json()["value"] == 8192
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        "llm.contract-host.smart_num_ctx",
    )
    assert row is not None, "No-DB carve-out must commit the row"
    assert row["value"] == 8192
    pending = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'llm.delivery_pending' AND user_id IS NULL",
    )
    assert pending is not None, "No-DB carve-out must record the pending role"
    assert "smart" in pending["value"]

    # -- Part 2: runtime key — delivery succeeds but _write_config_row fails --
    # Pins: delivery→commit ordering for LiteLLM runtime keys. When a PUT to
    # llm.smart_model delivers the new model to LiteLLM successfully but the
    # subsequent DB write fails, the PUT must raise (no committed row) and one
    # reconciler pass must re-deliver the STORED (old) model back to LiteLLM.
    monkeypatch.undo()

    import paper_ingestion.services.config_db as _config_db
    import paper_ingestion.services.litellm_config as _litellm_cfg
    from paper_ingestion.litellm_reconciler import _reconcile_litellm_models_once

    # Seed the "old" stored model so the reconciler has something to re-deliver.
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES (NULL, 'llm.smart_model', $1::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = $1::jsonb""",
        "qwen3:4b",
    )

    delivered_models: list[str] = []

    async def _capture_delivery(config_key: str, model_name: str, **kwargs: object) -> bool:  # noqa: ARG001
        delivered_models.append(model_name)
        return True

    # Stub LiteLLM HTTP calls so reconciler/delivery doesn't need a live proxy.
    async def _fake_deployments() -> list[dict]:
        return []

    monkeypatch.setattr(_litellm_cfg, "get_litellm_deployments", _fake_deployments)
    monkeypatch.setattr(_litellm_cfg, "update_litellm_model", _capture_delivery)
    # The PUT route binds update_litellm_model early (settings.py passes it as
    # update_litellm_model_fn) — patch the router namespace too.
    import paper_ingestion.routers.settings as _settings_router

    monkeypatch.setattr(_settings_router, "update_litellm_model", _capture_delivery)

    # Model-key PUTs verify the model against Ollama tags first; no Ollama here.
    async def _allow_model(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(_config_write, "validate_model_assignment", _allow_model)

    # Make _write_config_row fail after delivery has fired.
    _orig_write_config_row = _config_db._write_config_row

    write_call_count = 0

    async def _write_then_fail(conn, **kwargs: object) -> None:
        nonlocal write_call_count
        write_call_count += 1
        raise RuntimeError("db-write-fail")

    monkeypatch.setattr(_config_db, "_write_config_row", _write_then_fail)
    # config_write imports _write_config_row at its module level; patch there too.
    monkeypatch.setattr(_config_write, "_write_config_row", _write_then_fail)

    # PUT llm.smart_model = "qwen3:8b" — delivery fires (LiteLLM accepts) then
    # the row write fails. The ASGI test transport re-raises app exceptions, so
    # the failed PUT surfaces as the raw RuntimeError (a deployed server would
    # return 500); either way no row may be committed.
    with pytest.raises(RuntimeError, match="db-write-fail"):
        await pi_settings_client.put(
            "/api/config/llm.smart_model",
            json={"key": "llm.smart_model", "value": "qwen3:8b"},
        )
    # Delivery fired for the new model.
    assert "qwen3:8b" in delivered_models, (
        f"Delivery must have fired for 'qwen3:8b' before the write failed; delivered={delivered_models}"
    )
    # No row committed for the new value — old "qwen3:4b" row is intact.
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'llm.smart_model' AND user_id IS NULL"
    )
    assert row is not None, "Old stored model row must survive the failed PUT"
    assert str(row["value"]) == "qwen3:4b", (
        f"Old stored model must be 'qwen3:4b', got {row['value']!r}"
    )

    # Restore _write_config_row so the reconciler can write pending bookkeeping.
    monkeypatch.setattr(_config_db, "_write_config_row", _orig_write_config_row)
    monkeypatch.setattr(_config_write, "_write_config_row", _orig_write_config_row)

    # One reconciler pass must re-deliver the STORED (old) model "qwen3:4b".
    delivered_models.clear()
    shared = SharedConnPool(contract_conn)
    await _reconcile_litellm_models_once(shared)

    assert "qwen3:4b" in delivered_models, (
        f"Reconciler must re-deliver the stored model 'qwen3:4b' within one pass; "
        f"delivered={delivered_models}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_put_config_litellm_skipped_pending_semantics(
    contract_conn, pi_settings_client, monkeypatch
):
    """ "Skipped" delivery nuance for llm.delivery_pending.

    A MODEL-class key (llm.*_model) whose delivery is "skipped" means the alias
    already routes that exact model — truthfully applied, so the role is
    CLEARED from llm.delivery_pending (re-selecting the routed model must not
    leave a permanent false pill). A NUM_CTX-class key skipped on a cloud model
    says nothing about model delivery, so pending stays untouched.
    """
    import paper_ingestion.services.config_write as _config_write

    # Seed: smart is pending (e.g. a row committed under "No DB Connected").
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES (NULL, 'llm.delivery_pending', $1::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = $1::jsonb""",
        ["smart"],
    )

    async def _litellm_skipped(**kwargs):  # noqa: ARG001
        return "skipped"

    async def _allow_model(**kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(_config_write, "_apply_litellm_runtime_update", _litellm_skipped)
    # Bypass the outbound Ollama model-existence probe (idiomatic mock carve-out).
    monkeypatch.setattr(_config_write, "validate_model_assignment", _allow_model)

    # -- num_ctx skipped (cloud model) → pending NOT cleared ------------------
    resp = await pi_settings_client.put(
        "/api/config/llm.contract-host.smart_num_ctx",
        json={"key": "llm.contract-host.smart_num_ctx", "value": 4096},
    )
    assert resp.status_code == 200, f"num_ctx PUT failed: {resp.json()}"
    pending = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'llm.delivery_pending' AND user_id IS NULL",
    )
    assert pending is not None and pending["value"] == ["smart"], (
        f"num_ctx 'skipped' must NOT touch pending; got {pending and pending['value']!r}"
    )

    # -- model-role skipped (alias already routes it) → role cleared ----------
    resp = await pi_settings_client.put(
        "/api/config/llm.smart_model",
        json={"key": "llm.smart_model", "value": "qwen3:14b"},
    )
    assert resp.status_code == 200, f"model PUT failed: {resp.json()}"
    pending = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'llm.delivery_pending' AND user_id IS NULL",
    )
    assert pending is not None, "pending row must survive (emptied, not deleted)"
    assert "smart" not in pending["value"], (
        f"model-key 'skipped' must clear the role from pending; got {pending['value']!r}"
    )


# ---------------------------------------------------------------------------
# W1A.4 — settings/ai contract tests
#
# Verified: routers/settings_ai.py:53-73 (GET /api/settings/ai)
# Verified: routers/settings_ai.py:76-78 (POST /api/settings/ai/redetect)
# Verified: routers/settings_ai.py:81-101 (POST /api/settings/ai/dismiss-banner)
# Verified: services/ai_settings.py:133-208 (resolve_candidates_for_tier)
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
        "generated_at: '2026-07-01'\n"
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
    _sai_mod.observed_share = lambda _role: ("ollama_chat/qwen3:14b", 0.95)
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
    # eval_report_date reflects the YAML generated_at date, not the doc path.
    assert body["eval_report_date"] == "2026-07-01", (
        f"eval_report_date must be the generated_at date; got {body['eval_report_date']!r}"
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
# Verified: services/data_export.py (build_export_zip)
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
    Verified: services/data_export.py (build_export_zip returns bytes)
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


async def test_get_my_export_retains_current_and_stale_generation_rows(
    _me_export_client,
    contract_two_users,
    contract_conn,
):
    """The raw export preserves owner rows across source generations."""
    import io
    import json
    import zipfile

    user_id = contract_two_users.user_a_id
    paper_ids: list[int] = []
    for index in (1, 2):
        paper_id = await contract_conn.fetchval(
            """INSERT INTO papers (
                   external_id, source_type, title, authors, url, discovered_by,
                   content_generation
               )
               VALUES ($1, 'arxiv', $2, ARRAY['Author'], $3, $4, 2)
               RETURNING id""",
            f"raw-generation-{index}",
            f"Raw generation paper {index}",
            f"https://example.test/raw-generation-{index}",
            user_id,
        )
        paper_ids.append(int(paper_id))
        await contract_conn.execute(
            "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
            user_id,
            paper_id,
        )

    await contract_conn.executemany(
        """INSERT INTO paper_notes (
               paper_id, user_id, user_note, content_generation
           )
           VALUES ($1, $2, $3, $4)""",
        [
            (paper_ids[0], user_id, "RAW-NOTE-STALE", 1),
            (paper_ids[1], user_id, "RAW-NOTE-CURRENT", 2),
        ],
    )
    await contract_conn.executemany(
        """INSERT INTO paper_highlights (
               paper_id, user_id, page, rect, note, content_generation
           )
           VALUES ($1, $2, 1, $3, $4, $5)""",
        [
            (paper_ids[0], user_id, [0.1, 0.1, 0.2, 0.2], "RAW-HIGHLIGHT-STALE", 1),
            (paper_ids[1], user_id, [0.2, 0.2, 0.3, 0.3], "RAW-HIGHLIGHT-CURRENT", 2),
        ],
    )
    await contract_conn.executemany(
        """INSERT INTO cards (
               paper_id, user_id, card_type, front, back, content_generation
           )
           VALUES ($1, $2, 'concept', $3, 'back', $4)""",
        [
            (paper_ids[0], user_id, "RAW-CARD-STALE", 1),
            (paper_ids[1], user_id, "RAW-CARD-CURRENT", 2),
        ],
    )
    await contract_conn.executemany(
        """INSERT INTO paper_contradictions (
               paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
               contradiction_type, explanation, confidence, user_id,
               paper_a_content_generation, paper_b_content_generation
           )
           VALUES ($1, $2, $3, 'finding B', $4, $5, 'direct',
                   'generation export', 0.8, $6, $7, $7)""",
        [
            (
                paper_ids[0],
                paper_ids[1],
                "RAW-CONTRADICTION-STALE",
                "stale quote A",
                "stale quote B",
                user_id,
                1,
            ),
            (
                paper_ids[0],
                paper_ids[1],
                "RAW-CONTRADICTION-CURRENT",
                "current quote A",
                "current quote B",
                user_id,
                2,
            ),
        ],
    )

    response = await _me_export_client.get("/api/me/export")
    assert response.status_code == 200, response.text[:300]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        rows = {
            table: [
                json.loads(line)
                for line in archive.read(f"{table}.jsonl").decode().splitlines()
                if line
            ]
            for table in (
                "paper_notes",
                "paper_highlights",
                "cards",
                "paper_contradictions",
            )
        }

    assert {row["user_note"] for row in rows["paper_notes"] if row["paper_id"] in paper_ids} == {
        "RAW-NOTE-STALE",
        "RAW-NOTE-CURRENT",
    }
    assert {row["note"] for row in rows["paper_highlights"] if row["paper_id"] in paper_ids} == {
        "RAW-HIGHLIGHT-STALE",
        "RAW-HIGHLIGHT-CURRENT",
    }
    assert {row["front"] for row in rows["cards"] if row["paper_id"] in paper_ids} == {
        "RAW-CARD-STALE",
        "RAW-CARD-CURRENT",
    }
    contradictions = [
        row
        for row in rows["paper_contradictions"]
        if row["paper_a_id"] == paper_ids[0] and row["paper_b_id"] == paper_ids[1]
    ]
    assert {row["finding_a"] for row in contradictions} == {
        "RAW-CONTRADICTION-STALE",
        "RAW-CONTRADICTION-CURRENT",
    }
    assert {row["paper_a_content_generation"] for row in contradictions} == {1, 2}
    for table in ("paper_notes", "paper_highlights", "cards", "paper_contradictions"):
        assert all(row["user_id"] == user_id for row in rows[table])


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

    Closes a GDPR export-correctness gap: the
    happy-path test only checks status / content-type / non-empty body. A
    regression that passed the wrong user_id to ``build_export_zip`` (e.g.
    ``None`` or a hardcoded constant) would not be caught. Here we leverage the
    ``contract_two_users`` fixture which already seeds one paper per user with
    ``discovered_by`` set; we then GET as user A and assert user B's seeded
    paper is absent from ``papers.jsonl``.

    Verified: services/data_export.py — papers query is scoped via
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


async def test_non_admin_reads_pulse_flag_while_writes_stay_admin_only(
    contract_conn, pi_settings_client
):
    """A non-admin browser session can SEE that Pulse is off but not switch it.

    The empty-Pulse copy has to name the reason, which means the flag must reach
    a user who cannot change it. Readability and writability are separate gates:
    `list_config` consults BROWSER_READABLE_SYSTEM_KEYS while `set_config` still
    routes every system-scope key through require_admin.
    Verified: routers/settings.py list_config, services/config_metadata.py
    BROWSER_READABLE_SYSTEM_KEYS.
    """
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES (NULL, 'pulse.enabled', 'false'::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""",
    )

    resp = await pi_settings_client.get("/api/config")
    assert resp.status_code == 200
    entries = {item["key"]: item["value"] for item in resp.json()}
    assert entries.get("pulse.enabled") is False, (
        "a non-admin must see the flag, or the empty state cannot explain itself"
    )

    # The write gate is the key's classification, not this listing: making the
    # flag readable must not quietly make it personal, which is what would let a
    # non-admin write it. This fixture patches require_admin out, so asserting on
    # a rejected PUT here would prove nothing — the classification is the real
    # invariant, and it is what set_config branches on.
    from paper_ingestion.services.config_metadata import PERSONAL_KEYS, _classify_config_key

    assert _classify_config_key("pulse.enabled") == "system"
    assert "pulse.enabled" not in PERSONAL_KEYS
