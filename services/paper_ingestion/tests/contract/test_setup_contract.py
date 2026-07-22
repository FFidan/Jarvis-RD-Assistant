"""Setup wizard contract tests — target rows A131-A139.

Covers:
  A131: GET  /api/setup/status              — DB-driven configured flag
  A133: GET  /api/setup/smtp                — masked SMTP config from DB
  A134: POST /api/setup/smtp                — SMTP config persisted to user_config
  A135: POST /api/setup/admin               — first admin created; 409 if admin exists
  A136: POST /api/setup/cloud-llm-keys      — LLM keys stored encrypted in user_config
  A137: POST /api/setup/telegram-bot-token  — Telegram token stored encrypted
  A138: GET  /api/setup/telegram-bot-token  — has_token status from DB
  A139: POST /api/setup/mode                — setup_mode persisted to user_config

Skipped:
  A132: POST /api/setup/system-check — probes Ollama/Qdrant/LiteLLM external boundaries; IDIOMATIC-MOCK-ONLY carve-out.

Auth wiring:
  setup.py endpoints are registered with ``dependencies=[]`` (no global
  verify_api_key). Each endpoint calls ``require_unconfigured_or_admin()``
  directly — which is open when zero admins exist (bootstrap mode) and
  admin-only after an admin is seeded.

  Tests use bootstrap mode (no admin in DB) so no cookie is needed.
  A135 additionally tests the 409 path when an admin already exists, which
  requires seeding one first.

Carve-out: send_magic_link is patched; no outbound SMTP calls.
Carve-out: A135 creates a real session row but cookies are not followed
  (the test only asserts the DB side-effects and response shape).
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def _configure_config_key(monkeypatch):
    """Provide a real Fernet key for encrypted setup endpoints."""
    from jarvis_common.crypto import refresh_fernet_cache
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_CONFIG_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("JARVIS_CONFIG_KEY_OLD", raising=False)
    get_secrets_settings.cache_clear()
    refresh_fernet_cache()
    yield
    get_secrets_settings.cache_clear()
    refresh_fernet_cache()


_SETUP_TOKEN = "test-sentinel-token"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def setup_client(contract_conn, monkeypatch):
    """ASGI client for setup.py endpoints.

    No session cookie required — calls are made in bootstrap mode (no admin in DB).
    Sets BOTH pool overrides.  Disables rate limiter.  Configures the bootstrap
    setup token and sends it as the default ``X-Setup-Token`` header so the
    token-gated WRITE endpoints succeed.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.settings import get_secrets_settings
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    monkeypatch.setenv("JARVIS_SETUP_TOKEN", _SETUP_TOKEN)
    get_secrets_settings.cache_clear()

    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None},
            ),
        ):
            async with make_contract_client(app, None) as client:
                client.headers["X-Setup-Token"] = _SETUP_TOKEN
                yield client
    finally:
        app.state.limiter.enabled = True
        get_secrets_settings.cache_clear()


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def setup_app(contract_conn, monkeypatch):
    """Real setup app/pool wiring for controlled raw-peer transport tests."""
    from jarvis_common import verify_api_key
    from jarvis_common.settings import get_secrets_settings
    from jarvis_common.testing_contract_apps import patch_app_state, patch_dependency_overrides
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    monkeypatch.setenv("JARVIS_SETUP_TOKEN", _SETUP_TOKEN)
    get_secrets_settings.cache_clear()
    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None},
            ),
        ):
            yield app
    finally:
        app.state.limiter.enabled = True
        get_secrets_settings.cache_clear()


# ---------------------------------------------------------------------------
# A131: GET /api/setup/status — configured flag from DB
# ---------------------------------------------------------------------------


async def test_a131_status_unconfigured_when_no_admin(setup_client):
    """Covers map row A131: GET /api/setup/status returns configured=false when no admin in DB.

    Fresh contract DB has no users rows → configured=False.
    Verified: setup.py:246-261 get_status at HEAD.
    Survivor-of: test_setup_first_run.py status mock assertions.
    """
    resp = await setup_client.get("/api/setup/status")

    assert resp.status_code == 200, (
        f"Expected 200 from setup/status; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["configured"] is False, f"Expected configured=false on fresh DB; got: {body}"
    assert "setup_mode" in body


async def test_a131_status_configured_when_admin_exists(setup_client, contract_conn):
    """Covers map row A131: GET /api/setup/status returns configured=true once an admin exists.

    Verified: setup.py:258-261 admins > 0 branch at HEAD.
    """
    # Seed an admin user directly.
    await contract_conn.execute(
        "INSERT INTO users (email, role) VALUES ($1, 'admin')",
        "status-admin@example.com",
    )

    resp = await setup_client.get("/api/setup/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True, f"Expected configured=true after seeding admin; got: {body}"


async def test_a131_status_includes_setup_completed_false_when_configured_but_not_completed(
    setup_client, contract_conn
):
    """GAP-8: GET /api/setup/status includes setup_completed=False in the configured-but-not-completed state.

    The App.tsx gate reads BOTH configured AND setup_completed from this response.
    Guards that the combined shape is present — a regression where setup_completed
    is missing from the response would break the gate and re-show the wizard to
    authenticated users on every reload.

    Verified: setup.py get_status at HEAD (includes setup_completed from user_config).
    """
    # Seed an admin so the install is "configured" but leave setup.completed absent (= False).
    await contract_conn.execute(
        "INSERT INTO users (email, role) VALUES ($1, 'admin')",
        "gap8-admin@example.com",
    )

    resp = await setup_client.get("/api/setup/status")

    assert resp.status_code == 200, (
        f"Expected 200 from setup/status; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["configured"] is True, f"Expected configured=True after seeding admin; got: {body}"
    assert "setup_completed" in body, (
        f"setup_completed must be present in response when configured=true; got keys={list(body.keys())}"
    )
    assert body["setup_completed"] is False, (
        f"Expected setup_completed=False in configured-but-not-completed state; got: {body['setup_completed']!r}"
    )


# ---------------------------------------------------------------------------
# A133: GET /api/setup/smtp — masked SMTP config from DB
# ---------------------------------------------------------------------------


async def test_a133_smtp_config_reflects_persisted_rows(setup_client, contract_conn):
    """Covers map row A133: GET /api/setup/smtp returns host/port from user_config rows.

    Inserts plaintext SMTP rows directly into user_config, then asserts the
    endpoint returns them with the password masked.
    Verified: setup.py:447-461 get_smtp_config at HEAD.
    Survivor-of: test_setup_first_run.py smtp-get mock assertions.
    """
    # Insert SMTP config rows directly (bypass POST for isolation).
    for key, value in (("smtp.host", "mail.example.test"), ("smtp.port", 587)):
        await contract_conn.execute(
            """INSERT INTO user_config (user_id, key, value)
               VALUES (NULL, $1, $2::jsonb)
               ON CONFLICT (user_id, key) DO UPDATE SET value = $2::jsonb""",
            key,
            value,
        )

    resp = await setup_client.get("/api/setup/smtp")

    assert resp.status_code == 200, (
        f"Expected 200 from smtp GET; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["host"] == "mail.example.test", f"smtp.host mismatch: {body}"
    assert body["port"] == 587, f"smtp.port mismatch: {body}"
    # Password is never echoed.
    assert "pass" not in body, f"Password must not appear in response: {body}"
    assert "password" not in body, f"Password must not appear in response: {body}"


# ---------------------------------------------------------------------------
# A134: POST /api/setup/smtp — SMTP config written to user_config
# ---------------------------------------------------------------------------


async def test_a134_smtp_post_persists_to_db(setup_client, contract_conn, monkeypatch):
    """Covers map row A134: POST /api/setup/smtp persists smtp.host/port/from to user_config.

    Verified: setup.py:464-491 configure_smtp at HEAD.
    Survivor-of: test_setup_first_run.py smtp-post mock assertions.
    """
    # Bypass the SSRF guard so a placeholder hostname (non-resolvable in the
    # sandbox) doesn't block the persist path.  This test is about DB
    # persistence, not the SSRF guard.
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    resp = await setup_client.post(
        "/api/setup/smtp",
        json={
            "host": "smtp.contract.test",
            "port": 465,
            "from_email": "jarvis@contract.example.com",
            "test_send": False,
        },
    )

    assert resp.status_code == 200, (
        f"Expected 200 from smtp POST; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["saved"] is True

    # Verify rows in DB.
    host_row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'smtp.host' AND user_id IS NULL",
    )
    assert host_row is not None, "smtp.host row must exist in user_config after POST"
    assert host_row["value"] == "smtp.contract.test", (
        f"smtp.host mismatch in DB: {host_row['value']!r}"
    )

    from_row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'smtp.from' AND user_id IS NULL",
    )
    assert from_row is not None, "smtp.from row must exist in user_config after POST"
    assert from_row["value"] == "jarvis@contract.example.com"


# ---------------------------------------------------------------------------
# A135: POST /api/setup/admin — first admin creation
# ---------------------------------------------------------------------------


async def test_a135_create_first_admin_inserts_user_and_session(setup_client, contract_conn):
    """Covers map row A135: POST /api/setup/admin creates users row + sessions row.

    Fresh DB has no admins → endpoint is in bootstrap mode (no auth required).
    Verified: setup.py:494-578 create_first_admin at HEAD.
    Survivor-of: test_setup_first_run.py create-admin mock assertions.
    """
    resp = await setup_client.post(
        "/api/setup/admin",
        json={"email": "first-admin@example.com"},
    )

    assert resp.status_code == 200, (
        f"Expected 200 from create_first_admin; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["email"] == "first-admin@example.com"
    assert body["role"] == "admin"
    assert "id" in body

    user_id = body["id"]

    # Verify the user row in DB.
    row = await contract_conn.fetchrow(
        "SELECT role, deleted_at FROM users WHERE id = $1",
        user_id,
    )
    assert row is not None, "Admin user must exist in DB"
    assert row["role"] == "admin"
    assert row["deleted_at"] is None

    # Verify a sessions row was created (one-step wizard: no magic link required).
    session_row = await contract_conn.fetchrow(
        "SELECT id FROM sessions WHERE user_id = $1",
        user_id,
    )
    assert session_row is not None, "Session row must be created in sessions table"


async def test_a135_create_admin_409_when_admin_already_exists(setup_client, contract_conn):
    """Covers map row A135: 409 when an admin already exists.

    Verified: setup.py:521-532 admin_count > 0 guard at HEAD.
    """
    # Seed an existing admin.
    await contract_conn.execute(
        "INSERT INTO users (email, role) VALUES ($1, 'admin')",
        "existing-admin@example.com",
    )

    resp = await setup_client.post(
        "/api/setup/admin",
        json={"email": "second-admin@example.com"},
    )

    assert resp.status_code == 409, (
        f"Expected 409 when admin already exists; got {resp.status_code}: {resp.text[:300]}"
    )


async def test_setup_write_without_token_is_forbidden_over_http(setup_client):
    """Over the real ASGI route: a bootstrap WRITE with no valid token is 403.

    The other tests send the default ``X-Setup-Token`` header (positive path).
    This proves the gate END-TO-END over HTTP: with a token configured and no
    admin in DB (bootstrap mode), a setup WRITE that does not carry a valid
    token is rejected with 403 by ``require_unconfigured_or_admin`` — while the
    read-only status probe stays open.

    Verified: setup.py:286-322 _require_setup_token / require_unconfigured_or_admin
    (403 on missing/wrong token for a bootstrap POST) at HEAD.
    """
    # Read-only status probe stays open even when a token is configured.
    status_resp = await setup_client.get("/api/setup/status")
    assert status_resp.status_code == 200, (
        f"GET /api/setup/status must stay open without a token; "
        f"got {status_resp.status_code}: {status_resp.text[:300]}"
    )

    # A bootstrap WRITE carrying a WRONG token (overriding the fixture's valid
    # default header) must be rejected with 403, not persisted.
    resp = await setup_client.post(
        "/api/setup/admin",
        json={"email": "no-token-admin@example.com"},
        headers={"X-Setup-Token": "wrong-token"},
    )

    assert resp.status_code == 403, (
        f"Expected 403 for a bootstrap setup WRITE without a valid token; "
        f"got {resp.status_code}: {resp.text[:300]}"
    )


async def test_setup_admin_rejects_plaintext_lan_even_with_token_and_forged_headers(setup_app):
    """The transport gate runs before the first-admin transaction."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup_app, client=("203.0.113.7", 51234)),
        base_url="http://192.168.1.5",
        headers={"X-Setup-Token": _SETUP_TOKEN},
    ) as client:
        status_resp = await client.get("/api/setup/status")
        admin_resp = await client.post(
            "/api/setup/admin",
            json={"email": "plaintext-admin@example.com"},
            headers={
                "Host": "localhost",
                "Forwarded": "for=127.0.0.1;proto=https",
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Proto": "https",
                "X-Real-IP": "127.0.0.1",
            },
        )

    assert status_resp.status_code == 200
    assert admin_resp.status_code == 403, admin_resp.text
    assert "HTTPS" in admin_resp.json()["detail"]


# ---------------------------------------------------------------------------
# A136: POST /api/setup/cloud-llm-keys — LLM keys encrypted in user_config
# ---------------------------------------------------------------------------


async def test_a136_cloud_llm_keys_stored_encrypted(
    setup_client, contract_conn, _configure_config_key
):
    """Covers map row A136: POST /api/setup/cloud-llm-keys writes encrypted rows to user_config.

    Verifies that: (a) saved_providers reflects submitted keys,
    (b) an encrypted_value column is non-NULL in user_config.
    Verified: setup.py:581-667 configure_cloud_llm_keys at HEAD.
    Survivor-of: test_setup_first_run.py cloud-llm mock assertions.
    """
    resp = await setup_client.post(
        "/api/setup/cloud-llm-keys",
        json={"openai": "sk-contract-test-key-openai-xxxx"},
    )

    assert resp.status_code == 200, (
        f"Expected 200 from cloud-llm-keys; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert "openai" in body["saved_providers"], f"Expected 'openai' in saved_providers: {body}"

    # Verify the encrypted row exists in user_config.
    row = await contract_conn.fetchrow(
        "SELECT encrypted_value FROM user_config WHERE key = 'llm.openai.api_key' AND user_id IS NULL",
    )
    assert row is not None, "user_config row for llm.openai.api_key must exist"
    assert row["encrypted_value"] is not None, "LLM key must be stored encrypted"


# ---------------------------------------------------------------------------
# A137: POST /api/setup/telegram-bot-token — token stored encrypted
# ---------------------------------------------------------------------------


async def test_a137_telegram_token_stored_encrypted(
    setup_client, contract_conn, _configure_config_key
):
    """Covers map row A137: POST /api/setup/telegram-bot-token persists encrypted token.

    Verified: setup.py:670-688 configure_telegram_bot_token at HEAD.
    Survivor-of: test_setup_first_run.py telegram-token mock assertions.
    """
    resp = await setup_client.post(
        "/api/setup/telegram-bot-token",
        json={"token": "123456789:AAAAAAAAAA-BBBBBBBBBBB_CCCCCCCCCC"},
    )

    assert resp.status_code == 200, (
        f"Expected 200 from telegram-bot-token POST; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["saved"] is True
    assert body["restart_required"] is True

    # Verify encrypted row in user_config.
    row = await contract_conn.fetchrow(
        "SELECT encrypted_value FROM user_config WHERE key = 'telegram.bot_token' AND user_id IS NULL",
    )
    assert row is not None, "telegram.bot_token row must exist in user_config"
    assert row["encrypted_value"] is not None, "Token must be stored encrypted"


# ---------------------------------------------------------------------------
# A138: GET /api/setup/telegram-bot-token — has_token status
# ---------------------------------------------------------------------------


async def test_a138_telegram_token_status_reflects_db(setup_client, contract_conn):
    """Covers map row A138: GET /api/setup/telegram-bot-token returns has_token=true when row exists.

    Verified: setup.py:691-709 get_telegram_bot_token_status at HEAD.
    Survivor-of: test_setup_first_run.py telegram-status mock assertions.
    """
    # Initially no token.
    resp_before = await setup_client.get("/api/setup/telegram-bot-token")
    assert resp_before.status_code == 200
    assert resp_before.json()["has_token"] is False, "Expected has_token=false before insert"

    # Insert a fake encrypted value directly.
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value, encrypted_value)
           VALUES (NULL, 'telegram.bot_token', NULL, 'ZmFrZS1lbmNyeXB0ZWQ=')
           ON CONFLICT (user_id, key) DO UPDATE SET encrypted_value = 'ZmFrZS1lbmNyeXB0ZWQ='""",
    )

    resp_after = await setup_client.get("/api/setup/telegram-bot-token")
    assert resp_after.status_code == 200
    assert resp_after.json()["has_token"] is True, "Expected has_token=true after insert"


# ---------------------------------------------------------------------------
# A139: POST /api/setup/mode — setup_mode written to user_config
# ---------------------------------------------------------------------------


async def test_a139_setup_mode_persisted_to_db(setup_client, contract_conn):
    """Covers map row A139: POST /api/setup/mode writes setup.mode to user_config.

    Verified: setup.py:712-729 configure_setup_mode at HEAD.
    Survivor-of: test_setup_first_run.py setup-mode mock assertions.
    """
    resp = await setup_client.post(
        "/api/setup/mode",
        json={"mode": "multi"},
    )

    assert resp.status_code == 200, (
        f"Expected 200 from setup/mode; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["mode"] == "multi"
    # get_status reads the saved mode live (uncached) on the next poll, so the
    # mode change takes effect without a restart.
    assert body["restart_required"] is False

    # Verify user_config row.
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'setup.mode' AND user_id IS NULL",
    )
    assert row is not None, "setup.mode row must exist in user_config"
    assert row["value"] == "multi", f"Expected setup.mode='multi' in DB; got {row['value']!r}"


# ---------------------------------------------------------------------------
# E1.PI extensions — setup state multi-step progression + idempotency
#
# Verified: setup.py:246-261 (get_status — configured flag from users table)
# Verified: setup.py:494-578 (create_first_admin — users + sessions rows)
# Verified: setup.py:464-491 (configure_smtp — user_config UPSERT)
# ---------------------------------------------------------------------------


async def test_e1_setup_state_advances_after_admin_creation(setup_client, contract_conn):
    """GET /api/setup/status → configured=false; POST /api/setup/admin → status advances to true.

    Tests the multi-step operator-bootstrap flow: status reports false before an
    admin exists, and true immediately after the admin is created (same transaction scope).
    Verified: setup.py:246-261 (get_status) + setup.py:494-578 (create_first_admin).
    Survivor-of: test_setup_first_run.py multi-step state mock assertions.
    """
    # Pre-condition: no admin → configured=False
    resp_before = await setup_client.get("/api/setup/status")
    assert resp_before.status_code == 200
    assert resp_before.json()["configured"] is False

    # Create the first admin
    resp_admin = await setup_client.post(
        "/api/setup/admin",
        json={"email": "flow-admin@example.com"},
    )
    assert resp_admin.status_code == 200, f"Admin creation failed: {resp_admin.text[:200]}"

    # Post-condition: admin exists → configured=True
    resp_after = await setup_client.get("/api/setup/status")
    assert resp_after.status_code == 200
    assert resp_after.json()["configured"] is True, (
        "GET /api/setup/status must return configured=true after admin creation"
    )


async def test_e1_setup_smtp_idempotent_update(setup_client, contract_conn, monkeypatch):
    """POST /api/setup/smtp twice updates the same row (UPSERT idempotency).

    The second POST must overwrite smtp.host without creating a duplicate row.
    Verified: setup.py:464-491 (configure_smtp — ON CONFLICT DO UPDATE UPSERT path).
    Survivor-of: test_setup_first_run.py smtp idempotency tests.
    """
    # Bypass the SSRF guard so placeholder hostnames (non-resolvable in the
    # sandbox) don't block the persist path.  This test is about UPSERT
    # idempotency, not the SSRF guard.
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    await setup_client.post(
        "/api/setup/smtp",
        json={
            "host": "smtp-first.test",
            "port": 587,
            "from_email": "a@test.example.com",
            "test_send": False,
        },
    )
    await setup_client.post(
        "/api/setup/smtp",
        json={
            "host": "smtp-second.test",
            "port": 465,
            "from_email": "b@test.example.com",
            "test_send": False,
        },
    )

    # Only one row per key (UPSERT, not INSERT)
    count = await contract_conn.fetchval(
        "SELECT count(*) FROM user_config WHERE key = 'smtp.host' AND user_id IS NULL",
    )
    assert count == 1, f"Expected exactly 1 smtp.host row after two POSTs; got {count}"

    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'smtp.host' AND user_id IS NULL",
    )
    assert row["value"] == "smtp-second.test", (
        f"Second POST must overwrite smtp.host; got {row['value']!r}"
    )


async def test_e1_setup_status_contains_setup_mode(setup_client, contract_conn):
    """GET /api/setup/status includes setup_mode field (required by frontend wizard).

    Verified: setup.py:246-261 (get_status response shape includes setup_mode).
    Survivor-of: test_setup_first_run.py status response-shape assertions.
    """
    resp = await setup_client.get("/api/setup/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "setup_mode" in body, (
        f"GET /api/setup/status must include 'setup_mode' field; got keys={list(body.keys())}"
    )


async def test_smtp_test_send_rejects_private_host(setup_client, monkeypatch):
    """POST /api/setup/smtp with a private/link-local host is rejected at persist time (SSRF guard).

    The SSRF guard runs at persist time (``configure_smtp`` calls
    ``_reject_non_public_host`` before writing to the DB), so a private host
    is rejected with HTTP 422 before any aiosmtplib connection is attempted.
    This is the correct SSRF contract: a private host cannot be persisted at
    all, let alone used for a test send.

    Verified: setup.py configure_smtp (calls _reject_non_public_host before
    the DB write, gated by allow_private_smtp_host) at HEAD.
    """
    import aiosmtplib
    from unittest.mock import AsyncMock

    mock_send = AsyncMock(name="aiosmtplib.send")
    monkeypatch.setattr(aiosmtplib, "send", mock_send)

    resp = await setup_client.post(
        "/api/setup/smtp",
        json={
            "host": "169.254.169.254",
            "port": 80,
            "from_email": "a@b.com",
            "test_send": True,
            "test_recipient": "a@b.com",
        },
    )

    # The SSRF guard rejects the private host at persist time — 422, not 200.
    assert resp.status_code == 422, (
        f"Expected 422 (SSRF guard rejects private host); got {resp.status_code}: {resp.text[:300]}"
    )
    detail = resp.json().get("detail", "")
    assert "non-public" in detail, f"422 detail must mention 'non-public'; got: {detail!r}"
    # aiosmtplib.send must never have been called (guard fires before any connection).
    mock_send.assert_not_awaited()


async def test_e1_setup_admin_creates_session_and_user(setup_client, contract_conn):
    """POST /api/setup/admin creates both a users row AND a sessions row atomically.

    Duplicate of the positive path in test_a135_*, but independently verifies
    that both rows are committed atomically (no partial state on success).
    Verified: setup.py:540-570 (create_first_admin — INSERT sessions after INSERT users).
    Survivor-of: test_setup_first_run.py session-creation assertion.
    """
    resp = await setup_client.post(
        "/api/setup/admin",
        json={"email": "atomic-admin@example.com"},
    )
    assert resp.status_code == 200
    user_id = resp.json()["id"]

    user_count = await contract_conn.fetchval(
        "SELECT count(*) FROM users WHERE id = $1 AND role = 'admin'", user_id
    )
    session_count = await contract_conn.fetchval(
        "SELECT count(*) FROM sessions WHERE user_id = $1", user_id
    )
    assert user_count == 1, "users row must exist after admin creation"
    assert session_count == 1, "sessions row must be created atomically with the users row"


# ---------------------------------------------------------------------------
# [E] OWNER_USER_ID onboarding — first-admin auto-writes the owner.user_id row
# ---------------------------------------------------------------------------


async def test_create_first_admin_writes_owner_config_row(setup_client, contract_conn):
    """POST /api/setup/admin writes the owner.user_id system row == the new admin id.

    This is what lets owner API-key login work on a later multi-user box with no
    OWNER_USER_ID env set (the resolver falls back to this DB row).
    """
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY

    resp = await setup_client.post(
        "/api/setup/admin",
        json={"email": "owner-row-admin@example.com"},
    )
    assert resp.status_code == 200, f"admin creation failed: {resp.text[:300]}"
    user_id = resp.json()["id"]

    owner_value = await contract_conn.fetchval(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        OWNER_USER_ID_CONFIG_KEY,
    )
    assert owner_value == user_id, (
        f"owner.user_id row must equal the new admin id {user_id}; got {owner_value!r}"
    )
    audit = await contract_conn.fetchrow(
        "SELECT user_id, resource, metadata FROM audit_log WHERE action = 'admin.owner.bootstrap'"
    )
    assert audit is not None
    assert audit["user_id"] == str(user_id)
    assert audit["resource"] == "owner.user_id"
    assert audit["metadata"] == {"source": "first_admin"}


async def test_first_admin_owner_row_rolls_back_with_session_failure(
    setup_client, contract_conn, monkeypatch
):
    """The owner.user_id row is written INSIDE the create_first_admin transaction.

    Force the session INSERT (which runs after the owner UPSERT) to fail; the
    whole transaction rolls back, so neither the user nor the owner row persists.
    """
    import contextlib

    import jarvis_common.session_middleware as session_middleware
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY

    # first-admin mints via mint_session, whose ``now + SESSION_TTL`` raises a
    # TypeError when SESSION_TTL is not a timedelta — a deterministic failure
    # after the owner UPSERT but before the transaction commits.
    monkeypatch.setattr(session_middleware, "SESSION_TTL", "not-a-timedelta")

    with contextlib.suppress(Exception):
        await setup_client.post(
            "/api/setup/admin",
            json={"email": "rollback-admin@example.com"},
        )

    owner_value = await contract_conn.fetchval(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
        OWNER_USER_ID_CONFIG_KEY,
    )
    assert owner_value is None, "owner.user_id row must roll back with the failed transaction"
    user_row = await contract_conn.fetchval(
        "SELECT id FROM users WHERE email = $1",
        "rollback-admin@example.com",
    )
    assert user_row is None, "users row must roll back with the failed transaction"


async def test_first_admin_rolls_back_when_mandatory_owner_audit_fails(
    setup_client, contract_conn, monkeypatch
):
    """Bootstrap ownership and its security audit are one atomic mutation."""
    import contextlib

    from paper_ingestion.routers import setup as setup_router

    async def _fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(setup_router, "log_audit_strict", _fail_audit, raising=False)
    with contextlib.suppress(Exception):
        await setup_client.post(
            "/api/setup/admin",
            json={"email": "audit-rollback-admin@example.com"},
        )

    assert (
        await contract_conn.fetchval(
            "SELECT id FROM users WHERE email = 'audit-rollback-admin@example.com'"
        )
        is None
    )
    assert (
        await contract_conn.fetchval(
            "SELECT value FROM user_config WHERE user_id IS NULL AND key = 'owner.user_id'"
        )
        is None
    )


# ---------------------------------------------------------------------------
# §A1 — GET /api/setup/status: setup_completed field
# ---------------------------------------------------------------------------


async def test_a1_setup_status_setup_completed_false_on_fresh_db(setup_client):
    """GET /api/setup/status returns setup_completed=False when no user_config row.

    Fresh contract DB has no setup.completed row → _coerce_bool(None) → False.
    Verified: setup.py get_status at HEAD.
    """
    resp = await setup_client.get("/api/setup/status")

    assert resp.status_code == 200, (
        f"Expected 200 from setup/status; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert "setup_completed" in body, (
        f"GET /api/setup/status must include 'setup_completed' field; got keys={list(body.keys())}"
    )
    assert body["setup_completed"] is False, (
        f"Expected setup_completed=False on fresh DB; got: {body['setup_completed']!r}"
    )
    assert body["configured"] is False, (
        f"Expected configured=False on fresh DB; got: {body['configured']!r}"
    )


async def test_a1_setup_status_setup_completed_true_when_seeded(setup_client, contract_conn):
    """GET /api/setup/status returns setup_completed=True after user_config row is seeded.

    Inserts setup.completed=true directly, then verifies the endpoint reflects it.
    Verified: setup.py get_status reads user_config WHERE key='setup.completed'.
    """
    await contract_conn.execute(
        """INSERT INTO user_config (user_id, key, value)
           VALUES (NULL, 'setup.completed', $1::jsonb)
           ON CONFLICT (user_id, key) DO UPDATE SET value = $1::jsonb""",
        "true",
    )
    try:
        resp = await setup_client.get("/api/setup/status")

        assert resp.status_code == 200, (
            f"Expected 200 from setup/status; got {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
        assert body["setup_completed"] is True, (
            f"Expected setup_completed=True after seeding user_config; got: {body['setup_completed']!r}"
        )
    finally:
        await contract_conn.execute(
            "DELETE FROM user_config WHERE key = 'setup.completed' AND user_id IS NULL",
        )


# ---------------------------------------------------------------------------
# §T04 — GET /api/setup/status: smtp_configured field (Task T0.4)
# ---------------------------------------------------------------------------


async def test_t04_status_smtp_configured_false_on_fresh_db(setup_client):
    """GET /api/setup/status includes smtp_configured=False when no SMTP env or DB rows.

    Fresh contract DB has no smtp.host/smtp.from rows, and the test environment
    has no SMTP env vars → smtp_configured() returns False → the field is False.

    This field drives LoginPage's default tab: when False, the API-key tab is
    shown by default to avoid presenting a magic-link form that cannot deliver.

    Verified: setup.py get_status, jarvis_common.email.smtp_configured at HEAD.
    """
    resp = await setup_client.get("/api/setup/status")

    assert resp.status_code == 200, (
        f"Expected 200 from setup/status; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert "smtp_configured" in body, (
        f"GET /api/setup/status must include 'smtp_configured' field; got keys={list(body.keys())}"
    )
    # On a fresh DB with no SMTP env, this must be False.
    assert body["smtp_configured"] is False, (
        f"Expected smtp_configured=False on fresh DB with no SMTP env; got: {body['smtp_configured']!r}"
    )
    # smtp_reachable (liveness) is separate from configured (presence); a fresh
    # DB is not deliverable, so the probe returns False without any connection.
    assert "smtp_reachable" in body, (
        f"GET /api/setup/status must include 'smtp_reachable' field; got keys={list(body.keys())}"
    )
    assert body["smtp_reachable"] is False, (
        f"Expected smtp_reachable=False on a fresh, undeliverable DB; got: {body['smtp_reachable']!r}"
    )


async def test_t04_status_smtp_configured_true_when_db_rows_seeded(setup_client, contract_conn):
    """GET /api/setup/status returns smtp_configured=True when smtp.host + smtp.from are in user_config.

    Inserts the minimum SMTP rows (host + from) directly, then verifies the
    endpoint reflects smtp_configured=True.  User and password rows are NOT
    required — some relays use IP-allowlist auth.

    Verified: setup.py get_status + jarvis_common.email._smtp_configured at HEAD
    (deliverable = bool(host) and bool(sender)).
    """
    for key, value in (("smtp.host", "mail.example.test"), ("smtp.from", "jarvis@example.test")):
        await contract_conn.execute(
            """INSERT INTO user_config (user_id, key, value)
               VALUES (NULL, $1, $2::jsonb)
               ON CONFLICT (user_id, key) DO UPDATE SET value = $2::jsonb""",
            key,
            value,
        )
    try:
        resp = await setup_client.get("/api/setup/status")

        assert resp.status_code == 200, (
            f"Expected 200 from setup/status; got {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
        assert body["smtp_configured"] is True, (
            f"Expected smtp_configured=True after seeding smtp.host + smtp.from; "
            f"got: {body['smtp_configured']!r}"
        )
    finally:
        await contract_conn.execute(
            "DELETE FROM user_config WHERE key IN ('smtp.host', 'smtp.from') AND user_id IS NULL",
        )
