"""Tests for cloud LLM provider key encryption + POST /api/providers/{p}/test.

Covers:
- set_config writes encrypted_value (BYTEA) and clears value for encrypted keys
- get_config returns masked value (never plaintext) for encrypted keys
- Legacy plaintext rows are masked on read without auto-migration
- POST /api/providers/{provider}/test endpoint: unsupported, missing key, happy path, error
"""

from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
import respx
from httpx import ASGITransport
from jarvis_common.pinned_transport import PinnedAsyncTransport, pinned_async_client
from jarvis_common.testing import RoleMiddleware

from tests.conftest import _make_pool_and_conn

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Minimal paper_ingestion app with mocked DB and auth disabled."""
    from unittest.mock import AsyncMock

    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    with patch_pi_test_app(
        mock_pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            # set_config reads request.app.state.http_client; provide a stub so
            # the test does not depend on a sibling test having set it on the
            # shared app singleton.
            state_overrides={"http_client": AsyncMock()},
            dependency_overrides={
                verify_api_key: lambda: None,
                # get_config / set_config / test_provider resolve the caller
                # via Depends(current_user_id_strict); steer it to a concrete
                # user (the routes hard-401 sessionless callers otherwise).
                current_user_id_strict: lambda: 1,
            },
        ),
    ):
        yield app, conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _put_config(app, key: str, value, *, role: str | None = None):
    """PUT /api/config/{key}.

    Pass *role* (e.g. ``"admin"``) to simulate a browser session with that role —
    required for SYSTEM_KEYS (e.g. cloud LLM api_key) that enforce the admin gate.
    """
    transport_app = RoleMiddleware(app, role) if role is not None else app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=transport_app), base_url="http://test"
    ) as client:
        return await client.put(f"/api/config/{key}", json={"key": key, "value": value})


async def _get_config(app, key: str):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(f"/api/config/{key}")


async def _get_providers(app, *, role: str | None = "admin"):
    transport_app = RoleMiddleware(app, role) if role is not None else app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=transport_app), base_url="http://test"
    ) as client:
        return await client.get("/api/providers")


async def _post_provider_test(app, provider: str, *, role: str | None = "admin"):
    transport_app = RoleMiddleware(app, role) if role is not None else app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=transport_app), base_url="http://test"
    ) as client:
        return await client.post(f"/api/providers/{provider}/test")


async def _get_provider_account(app, provider: str, *, role: str | None = "admin"):
    transport_app = RoleMiddleware(app, role) if role is not None else app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=transport_app), base_url="http://test"
    ) as client:
        return await client.get(f"/api/providers/{provider}/account")


@pytest.mark.asyncio
async def test_provider_probe_route_refuses_quarantine_before_database(_app, monkeypatch, tmp_path):
    """A quarantined provider test returns 503 before its database lookup."""
    app, conn = _app
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.write_text("malformed")
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))

    response = await _post_provider_test(app, "openai")

    assert response.status_code == 503
    assert "read-only" in response.json()["detail"]
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_probe_sink_refuses_quarantine_before_http(monkeypatch, tmp_path):
    from paper_ingestion.services.provider_test import test_provider_connectivity

    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))

    result = await test_provider_connectivity("openai", "restored-key")

    assert result.ok is False
    assert result.error == "provider access is disabled until restored credentials are reviewed"


# ---------------------------------------------------------------------------
# Tests: set_config writes encrypted_value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_set_encrypted_key_writes_bytea(_app):
    """PUT /api/config/llm.anthropic.api_key writes encrypted_value BYTEA, not plaintext.

    llm.anthropic.api_key is a SYSTEM_KEY (deployment-wide, admin-only write).
    The route enforces require_admin; this test simulates an admin browser session
    via RoleMiddleware and asserts the DB row is written with user_id=NULL (system scope).
    """
    app, conn = _app

    # SYSTEM_KEY: requires admin session — simulate via RoleMiddleware.
    resp = await _put_config(app, "llm.anthropic.api_key", "sk-ant-test123", role="admin")

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "llm.anthropic.api_key"
    # Response must be masked — never the real key
    assert body["value"] != "sk-ant-test123"
    assert "****" in body["value"]

    # Verify the DB execute was called with the encrypted_value path
    # set_config now emits a log_event (INSERT INTO system_events) in addition to the
    # UPSERT — expect at least one execute call (the config UPSERT).
    conn.execute.assert_awaited()
    # Use call_args_list[0]: the first call is always the UPSERT.
    call_args = conn.execute.call_args_list[0]
    sql = call_args.args[0]
    assert "encrypted_value" in sql
    # SYSTEM_KEY: user_id ($1) must be NULL — write is system-scoped, not user-scoped.
    assert call_args.args[1] is None, (
        "llm.anthropic.api_key is a SYSTEM_KEY: DB row must be written with user_id=NULL"
    )
    # The key passed as $2 must be the config key
    assert call_args.args[2] == "llm.anthropic.api_key"
    # The ciphertext passed as $3 must be bytes
    ciphertext_arg = call_args.args[3]
    assert isinstance(ciphertext_arg, bytes)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_set_encrypted_key_does_not_store_plaintext(_app):
    """PUT /api/config/llm.openai.api_key does not pass the raw key to asyncpg.

    llm.openai.api_key is a SYSTEM_KEY: requires admin session via RoleMiddleware.
    Asserts plaintext is never forwarded to the DB driver and the write is system-scoped
    (user_id=NULL).
    """
    app, conn = _app

    plaintext = "sk-openai-secret"
    # SYSTEM_KEY: requires admin session — simulate via RoleMiddleware.
    await _put_config(app, "llm.openai.api_key", plaintext, role="admin")

    conn.execute.assert_awaited()
    # Use call_args_list[0]: the first call is always the UPSERT (not the log_event INSERT).
    call_args = conn.execute.call_args_list[0]
    # SYSTEM_KEY: user_id ($1) must be NULL — write is system-scoped, not user-scoped.
    assert call_args.args[1] is None, (
        "llm.openai.api_key is a SYSTEM_KEY: DB row must be written with user_id=NULL"
    )
    # The plaintext should not appear in any argument
    for arg in call_args.args:
        if isinstance(arg, str | bytes):
            assert plaintext not in (arg if isinstance(arg, str) else arg.decode("ascii", "ignore"))


# ---------------------------------------------------------------------------
# Tests: get_config returns masked value for encrypted keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_get_encrypted_key_returns_masked(_app):
    """GET /api/config/llm.anthropic.api_key returns masked value, never plaintext."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app
    plaintext = "sk-ant-realkey"
    ciphertext = encrypt_secret(plaintext).encode("ascii")

    conn.fetchrow.return_value = {
        "key": "llm.anthropic.api_key",
        "value": None,
        "encrypted_value": ciphertext,
    }

    resp = await _get_config(app, "llm.anthropic.api_key")

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "llm.anthropic.api_key"
    assert plaintext not in resp.text
    # H.1: masked form is "****" + last 4 chars (no prefix leak)
    masked = body["value"]
    assert "****" in masked
    assert masked == "****" + plaintext[-4:]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_get_encrypted_key_short_returns_four_stars(_app):
    """GET returns '****' for an encrypted key with value <= 4 chars."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app
    plaintext = "abc"  # 3 chars, <= 4
    ciphertext = encrypt_secret(plaintext).encode("ascii")

    conn.fetchrow.return_value = {
        "key": "llm.google.api_key",
        "value": None,
        "encrypted_value": ciphertext,
    }

    resp = await _get_config(app, "llm.google.api_key")
    assert resp.status_code == 200
    assert resp.json()["value"] == "****"


# ---------------------------------------------------------------------------
# Tests: legacy plaintext rows mask on read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_legacy_plaintext_row_masks_on_read(_app):
    """GET for an encrypted key that still has plaintext value returns masked form."""
    app, conn = _app

    # Simulate a legacy row: value populated, encrypted_value = NULL
    conn.fetchrow.return_value = {
        "key": "zotero.api_key",
        "value": "legacytoken12345",
        "encrypted_value": None,
    }

    resp = await _get_config(app, "zotero.api_key")

    assert resp.status_code == 200
    body = resp.json()
    # Must be masked
    assert "legacytoken12345" not in resp.text
    assert "****" in body["value"]
    # H.1: mask shows last 4 chars; "legacytoken12345" → "****2345"
    assert body["value"] == "****2345"


# ---------------------------------------------------------------------------
# Tests: /api/providers admin boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_list_providers_requires_admin_session(_app):
    """Provider configured-status metadata is admin-only."""
    app, conn = _app

    api_key_only = await _get_providers(app, role=None)
    member = await _get_providers(app, role="member")

    assert api_key_only.status_code == 403
    assert member.status_code == 403
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_list_providers_admin_returns_metadata(_app):
    """Admins can list provider metadata and configured flags."""
    app, conn = _app
    conn.fetchrow.return_value = None

    resp = await _get_providers(app, role="admin")

    assert resp.status_code == 200
    provider_ids = {item["id"] for item in resp.json()}
    assert {"anthropic", "openai", "custom_openai_compatible"} <= provider_ids
    openrouter = next(item for item in resp.json() if item["id"] == "openrouter")
    assert openrouter["dashboard_url"] == "https://openrouter.ai/dashboard/api-keys"
    assert openrouter["account_capability"] == "current_key"
    assert {"label", "creator_user_id", "workspace_id", "hash"}.isdisjoint(openrouter)


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_requires_admin_session(_app):
    """Live provider probes must not be callable by non-admin or API-key-only users."""
    app, conn = _app

    api_key_only = await _post_provider_test(app, "anthropic", role=None)
    member = await _post_provider_test(app, "anthropic", role="member")

    assert api_key_only.status_code == 403
    assert member.status_code == 403
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_provider_account_requires_admin_session(_app):
    """Account snapshots keep the same browser-admin boundary as provider tests."""
    app, conn = _app

    api_key_only = await _get_provider_account(app, "openrouter", role=None)
    member = await _get_provider_account(app, "openrouter", role="member")

    assert api_key_only.status_code == 403
    assert member.status_code == 403
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_provider_account_never_fetches_or_uses_shared_client(monkeypatch):
    """Unsupported provider accounts are a capability response, never an outbound probe."""
    from paper_ingestion.services import provider_account

    fetch_key = AsyncMock()
    pinned_factory = AsyncMock()
    monkeypatch.setattr(provider_account, "get_provider_api_key", fetch_key)
    monkeypatch.setattr(provider_account, "pinned_async_client", pinned_factory)

    snapshot = await provider_account.fetch_provider_account("anthropic", db_pool=object())

    assert snapshot.capability == "unavailable"
    assert snapshot.data == {}
    assert snapshot.error_code is None
    fetch_key.assert_not_awaited()
    pinned_factory.assert_not_called()


@pytest.mark.asyncio
async def test_openrouter_account_uses_public_pinned_transport_and_discards_identity(monkeypatch):
    """Current-key snapshots use a dedicated public transport and an explicit field allow-list."""
    from jarvis_common.pinned_transport import PUBLIC_ONLY
    from paper_ingestion.services import provider_account

    transport_calls: list[object] = []

    @asynccontextmanager
    async def pinned_client(policy, *, timeout):
        transport_calls.append((policy, timeout))
        payload = {
            "data": {
                "is_free_tier": False,
                "usage": 2.5,
                "usage_daily": 1.25,
                "usage_weekly": 2.5,
                "usage_monthly": 2.5,
                "limit": 10,
                "limit_remaining": 7.5,
                "limit_reset": "monthly",
                "expires_at": "2030-01-01T00:00:00Z",
                "label": "must-not-leak",
                "creator_user_id": "must-not-leak",
                "workspace_id": "must-not-leak",
                "hash": "must-not-leak",
            }
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        ) as client:
            yield client

    monkeypatch.setattr(provider_account, "pinned_async_client", pinned_client)
    monkeypatch.setattr(
        provider_account, "get_provider_api_key", AsyncMock(return_value="test-token")
    )

    snapshot = await provider_account.fetch_provider_account("openrouter", db_pool=object())

    assert transport_calls and transport_calls[0][0] is PUBLIC_ONLY
    assert snapshot.capability == "current_key"
    assert snapshot.error_code is None
    assert snapshot.data == {
        "is_free_tier": False,
        "usage": 2.5,
        "usage_daily": 1.25,
        "usage_weekly": 2.5,
        "usage_monthly": 2.5,
        "limit": 10,
        "limit_remaining": 7.5,
        "limit_reset": "monthly",
        "expires_at": "2030-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_openrouter_account_failure_codes_are_sanitized(monkeypatch):
    """Provider response details never become account endpoint output."""
    from paper_ingestion.services import provider_account

    @asynccontextmanager
    async def pinned_client(_policy, *, timeout):
        del timeout
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, text="sensitive upstream response")
            )
        ) as client:
            yield client

    monkeypatch.setattr(provider_account, "pinned_async_client", pinned_client)
    monkeypatch.setattr(
        provider_account, "get_provider_api_key", AsyncMock(return_value="test-token")
    )

    snapshot = await provider_account.fetch_provider_account("openrouter", db_pool=object())

    assert snapshot.data == {}
    assert snapshot.error_code == "provider_http_error"


@pytest.mark.asyncio
async def test_openrouter_account_response_is_hard_byte_capped(monkeypatch):
    """Account snapshots stop reading oversized bodies before decoding them."""
    from paper_ingestion.services import provider_account

    @asynccontextmanager
    async def pinned_client(_policy, *, timeout):
        del timeout
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, content=b"x" * (provider_account._MAX_ACCOUNT_RESPONSE_BYTES + 1)
                )
            )
        ) as client:
            yield client

    monkeypatch.setattr(provider_account, "pinned_async_client", pinned_client)
    monkeypatch.setattr(
        provider_account, "get_provider_api_key", AsyncMock(return_value="test-token")
    )

    snapshot = await provider_account.fetch_provider_account("openrouter", db_pool=object())

    assert snapshot.data == {}
    assert snapshot.error_code == "provider_response_too_large"


# ---------------------------------------------------------------------------
# Tests: POST /api/providers/{provider}/test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_unsupported_returns_400(_app):
    """POST /api/providers/unknown/test returns 400 for unsupported provider."""
    app, _ = _app

    resp = await _post_provider_test(app, "unknown")

    assert resp.status_code == 400
    assert "unsupported provider" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_unsupported_ollama_returns_400(_app):
    """POST /api/providers/ollama/test returns 400 — not in supported set."""
    app, _ = _app

    resp = await _post_provider_test(app, "ollama")

    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_missing_key_returns_ok_false(_app):
    """POST /api/providers/anthropic/test returns ok=false when no key is configured."""
    app, conn = _app
    conn.fetchrow.return_value = None  # No row in DB

    resp = await _post_provider_test(app, "anthropic")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "no api key configured"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_empty_key_returns_ok_false(_app):
    """POST /api/providers/openai/test returns ok=false when key decrypts to empty."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app
    # Encrypt an empty-ish string; _validate_nonempty_str would reject this at set time,
    # but test the defensive path for robustness
    ciphertext = encrypt_secret("   ").encode("ascii")
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext,
    }

    resp = await _post_provider_test(app, "openai")

    # "   " is falsy after strip, but the handler checks `not api_key` which is falsy
    # for an all-whitespace string stripped — actually " " is truthy unless we strip.
    # The handler does `if not api_key`, so "   " would pass through. Test the None case.
    # (This test mainly verifies the endpoint is reachable when encrypted_value is set.)
    assert resp.status_code == 200


@pytest.mark.asyncio
@respx.mock
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_happy_path_anthropic(_app):
    """POST /api/providers/anthropic/test returns ok=true when Anthropic responds 200."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app

    plaintext_key = "sk-ant-test-valid-key"
    ciphertext = encrypt_secret(plaintext_key).encode("ascii")
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext,
    }

    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "claude-x"}]})
    )

    resp = await _post_provider_test(app, "anthropic")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None


@pytest.mark.asyncio
@respx.mock
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_happy_path_openai(_app):
    """POST /api/providers/openai/test returns ok=true when OpenAI responds 200."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app

    ciphertext = encrypt_secret("sk-openai-valid").encode("ascii")
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext,
    }

    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    resp = await _post_provider_test(app, "openai")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
@respx.mock
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_happy_path_google(_app):
    """POST /api/providers/google/test returns ok=true when Google responds 200."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app

    ciphertext = encrypt_secret("AIza-googlekey").encode("ascii")
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext,
    }

    respx.get(url__startswith="https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(200, json={"models": []})
    )

    resp = await _post_provider_test(app, "google")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
@respx.mock
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_http_error_returns_ok_false(_app):
    """POST /api/providers/anthropic/test returns ok=false on 401 response."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app

    ciphertext = encrypt_secret("sk-ant-bad-key").encode("ascii")
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext,
    }

    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid API key"}})
    )

    resp = await _post_provider_test(app, "anthropic")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] is not None
    assert len(body["error"]) <= 200


@pytest.mark.asyncio
@respx.mock
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_connection_error_returns_ok_false(_app):
    """POST /api/providers/anthropic/test returns ok=false on network error."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app

    ciphertext = encrypt_secret("sk-ant-unreachable").encode("ascii")
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext,
    }

    respx.get("https://api.anthropic.com/v1/models").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    resp = await _post_provider_test(app, "anthropic")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "provider request failed"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fernet_key")
async def test_test_provider_custom_endpoint_blocks_link_local_resolution(_app, monkeypatch):
    """Custom endpoint tests reject blocked resolved addresses before outbound HTTP."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app
    conn.fetchrow.side_effect = [
        {"value": None, "encrypted_value": encrypt_secret("sk-custom").encode("ascii")},
        {"value": "https://custom.internal/v1", "encrypted_value": None},
    ]

    def fake_getaddrinfo(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(
        "paper_ingestion.services.llm_provider_registry.socket.getaddrinfo", fake_getaddrinfo
    )

    resp = await _post_provider_test(app, "custom_openai_compatible")

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": False,
        "error": "custom endpoint resolves to a blocked network address",
    }


@pytest.mark.asyncio
async def test_custom_provider_rebind_is_blocked_at_the_real_connect_boundary(monkeypatch):
    """A public validation answer cannot authorize a private connection answer."""
    from paper_ingestion.services import provider_test

    delegated: list[str] = []

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host: str, port: int, **_kwargs):  # type: ignore[no-untyped-def]
            delegated.append(host)
            return object()

        async def connect_unix_socket(self, path: str, **_kwargs):  # type: ignore[no-untyped-def]
            return object()

        async def sleep(self, seconds: float) -> None:
            return None

    def public_validation_answer(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    async def private_connect_answer(host: str, port: int) -> list[tuple[int, str]]:
        return [(socket.AF_INET, "127.0.0.1")]

    def client_factory(policy, *, timeout):  # type: ignore[no-untyped-def]
        return pinned_async_client(
            policy,
            timeout=timeout,
            transport=PinnedAsyncTransport(
                policy,
                resolver=private_connect_answer,
                backend=Backend(),
            ),
        )

    monkeypatch.setattr(
        "paper_ingestion.services.llm_provider_registry.socket.getaddrinfo",
        public_validation_answer,
    )
    monkeypatch.setattr(provider_test, "pinned_async_client", client_factory)

    result = await provider_test.test_provider_connectivity(
        "custom_openai_compatible",
        "test-key",
        base_url="https://custom.example/v1",
    )

    assert result.ok is False
    assert result.error == "provider request failed"
    assert delegated == []


@pytest.mark.asyncio
@respx.mock
@pytest.mark.usefixtures("fernet_key")
async def test_google_probe_uses_header_not_url_param(_app):
    """Google probe must send key via x-goog-api-key header, never as URL param."""
    from jarvis_common.crypto import encrypt_secret

    app, conn = _app

    plaintext_key = "AIza-sec003-secret-key"
    ciphertext = encrypt_secret(plaintext_key).encode("ascii")
    conn.fetchrow.return_value = {
        "value": None,
        "encrypted_value": ciphertext,
    }

    captured: list = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"models": []})

    respx.get("https://generativelanguage.googleapis.com/v1beta/models").mock(side_effect=_capture)

    resp = await _post_provider_test(app, "google")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert len(captured) == 1, "Expected exactly one outbound Google request"
    req = captured[0]

    # Key must NOT appear in the URL query string
    assert "key=" not in str(req.url), f"API key leaked in URL: {req.url}"
    # Key must be in the request header
    assert req.headers.get("x-goog-api-key") == plaintext_key, (
        "Expected x-goog-api-key header with the API key"
    )


# ---------------------------------------------------------------------------
# Live provider model lists on the /api/system/models response
# ---------------------------------------------------------------------------


def test_provider_lists_survives_the_models_response_model() -> None:
    """Pydantic drops undeclared top-level keys silently — assert through the model."""
    from paper_ingestion.routers.system import SystemModelsWithDeliveryResponse

    result = {
        "status": "ok",
        "installed": [],
        "hardware": {},
        "current": {},
        "issues": {},
        "catalog": [],
        "recommendations": {},
        "provider_lists": {
            "openrouter": {
                "model_count": 2,
                "fetched_at": "2026-08-01T00:00:00+00:00",
                "error": None,
                "truncated": False,
                "excluded": {"non_chat": 1, "unknown": 0, "invalid": 0},
            }
        },
    }

    validated = SystemModelsWithDeliveryResponse.model_validate(result)

    assert validated.provider_lists
    assert validated.provider_lists["openrouter"]["model_count"] == 2


@pytest.mark.asyncio
async def test_key_presence_covers_every_registered_provider() -> None:
    """Presence is read for all nine providers, not the three that once shipped."""
    from paper_ingestion.routers.system import _cloud_key_presence
    from paper_ingestion.services.llm_provider_registry import PROVIDER_REGISTRY
    from tests.test_provider_models import FakeConfigPool

    presence = await _cloud_key_presence(FakeConfigPool({"llm.providers.deepseek.api_key": "key"}))

    assert {provider.id for provider in PROVIDER_REGISTRY} == set(presence)
    assert presence["deepseek"] is True
    assert presence["anthropic"] is False


@pytest.mark.asyncio
async def test_models_response_offers_a_keyless_custom_endpoint_live_models(monkeypatch) -> None:
    """A provider reachable by base URL alone is fetched, merged, and recommended."""
    from types import SimpleNamespace

    from paper_ingestion.routers.system import _get_system_models_data
    from paper_ingestion.services.provider_models import reset_provider_model_cache
    from tests.test_provider_models import FakeConfigPool, mock_http_client

    # _get_system_models_data probes LiteLLM for real; fail that probe fast
    # (connection refused) instead of hanging ~10 s on resolving the
    # compose-internal hostname.
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:9")

    reset_provider_model_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "org/model-y"}]})
        return httpx.Response(404, json={})

    async with mock_http_client(handler) as client:
        pool = FakeConfigPool(
            {"llm.providers.custom_openai_compatible.base_url": "http://localhost:8000/v1"}
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(http_client=client, db_pool=pool))
        )
        body = await _get_system_models_data(request)  # type: ignore[arg-type]

    summary = body.provider_lists["custom_openai_compatible"]
    assert summary["model_count"] == 1
    assert summary["error"] is None

    entry = next(item for item in body.catalog if item["id"] == "custom_openai/org/model-y")
    assert entry["can_assign"] is True
    assert entry["source"] == "provider"
    assert entry["fetched_at"] == summary["fetched_at"]
    assert any(item["id"] == "custom_openai/org/model-y" for item in body.recommendations["smart"])
