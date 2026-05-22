"""Setup wizard contract tests — Phase B target rows A131-A139.

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

import pytest
import pytest_asyncio
import httpx
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


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def setup_client(contract_conn):
    """ASGI client for setup.py endpoints.

    No session cookie required — calls are made in bootstrap mode (no admin in DB).
    Sets BOTH pool overrides.  Disables rate limiter.
    """
    from paper_ingestion.main import app
    from paper_ingestion.deps import get_db_pool
    from jarvis_common import verify_api_key

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)

    app.state.db_pool = shared
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[verify_api_key] = lambda: None
    app.state.limiter.enabled = False

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        if original_pool is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original_pool
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(verify_api_key, None)
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# A131: GET /api/setup/status — configured flag from DB
# ---------------------------------------------------------------------------


async def test_a131_status_unconfigured_when_no_admin(setup_client):
    """Covers map row A131: GET /api/setup/status returns configured=false when no admin in DB.

    Fresh contract DB has no users rows → configured=False.
    Verified: setup.py:246-261 get_status at HEAD.
    Survivor-of (future Phase C): test_setup_first_run.py status mock assertions.
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


# ---------------------------------------------------------------------------
# A133: GET /api/setup/smtp — masked SMTP config from DB
# ---------------------------------------------------------------------------


async def test_a133_smtp_config_reflects_persisted_rows(setup_client, contract_conn):
    """Covers map row A133: GET /api/setup/smtp returns host/port from user_config rows.

    Inserts plaintext SMTP rows directly into user_config, then asserts the
    endpoint returns them with the password masked.
    Verified: setup.py:447-461 get_smtp_config at HEAD.
    Survivor-of (future Phase C): test_setup_first_run.py smtp-get mock assertions.
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


async def test_a134_smtp_post_persists_to_db(setup_client, contract_conn):
    """Covers map row A134: POST /api/setup/smtp persists smtp.host/port/from to user_config.

    Verified: setup.py:464-491 configure_smtp at HEAD.
    Survivor-of (future Phase C): test_setup_first_run.py smtp-post mock assertions.
    """
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
    Survivor-of (future Phase C): test_setup_first_run.py create-admin mock assertions.
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
    Survivor-of (future Phase C): test_setup_first_run.py cloud-llm mock assertions.
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
    Survivor-of (future Phase C): test_setup_first_run.py telegram-token mock assertions.
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
    Survivor-of (future Phase C): test_setup_first_run.py telegram-status mock assertions.
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
    Survivor-of (future Phase C): test_setup_first_run.py setup-mode mock assertions.
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
    assert body["restart_required"] is True

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
    Survivor-of (Phase E2): test_setup_first_run.py multi-step state mock assertions.
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


async def test_e1_setup_smtp_idempotent_update(setup_client, contract_conn):
    """POST /api/setup/smtp twice updates the same row (UPSERT idempotency).

    The second POST must overwrite smtp.host without creating a duplicate row.
    Verified: setup.py:464-491 (configure_smtp — ON CONFLICT DO UPDATE UPSERT path).
    Survivor-of (Phase E2): test_setup_first_run.py smtp idempotency tests.
    """
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
    Survivor-of (Phase E2): test_setup_first_run.py status response-shape assertions.
    """
    resp = await setup_client.get("/api/setup/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "setup_mode" in body, (
        f"GET /api/setup/status must include 'setup_mode' field; got keys={list(body.keys())}"
    )


async def test_e1_setup_admin_creates_session_and_user(setup_client, contract_conn):
    """POST /api/setup/admin creates both a users row AND a sessions row atomically.

    Duplicate of the positive path in test_a135_*, but independently verifies
    that both rows are committed atomically (no partial state on success).
    Verified: setup.py:540-570 (create_first_admin — INSERT sessions after INSERT users).
    Survivor-of (Phase E2): test_setup_first_run.py session-creation assertion.
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
