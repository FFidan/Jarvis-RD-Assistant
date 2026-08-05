"""Tests for H-2: secret-in-logs + prod-safety.

Covers:
- send_magic_link never logs the raw magic-link or token
- send_magic_link logs email_hash (SHA-256) + link_issued=true instead
- log_event context never contains the raw link
- validate_production_config raises when ENVIRONMENT=production, DEV_SMTP_LOG_ONLY=false,
  and SMTP is not configured
- validate_production_config passes when all SMTP fields are present
- Non-production environments are not affected by the SMTP check
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from jarvis_common.app_factory import configure_middleware_and_errors
from jarvis_common.auth import validate_production_config, validate_runtime_config
from jarvis_common.testing import make_pool_and_conn
from slowapi import Limiter
from slowapi.util import get_remote_address

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN = "supersecrettoken123"
_EMAIL = "test@example.com"
_LINK = f"https://localhost:3001/auth/verify?token={_TOKEN}"


def _email_sha256(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DEV_MODE",
        "DEV_AUTH_BYPASS",
        "DEV_ERROR_DETAIL",
        "DEV_CORS_OPEN",
        "DEV_SMTP_LOG_ONLY",
        "DEV_CRYPTO_RELAXED",
        "ENVIRONMENT",
        "JARVIS_API_KEY",
        "JARVIS_MODEL_HMAC_KEY",
        "JARVIS_CONFIG_KEY",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_FROM",
        "SMTP_USER",
        "SMTP_PASS",
        "LITELLM_MASTER_KEY",
        "POSTGRES_PASSWORD",
        "APP_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _minimal_prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimum valid env vars for production (no SMTP yet)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JARVIS_API_KEY", "a" * 32)
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "b" * 32)
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "c" * 44)  # valid Fernet-length
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-" + "d" * 40)  # requires strength validation
    monkeypatch.setenv("POSTGRES_PASSWORD", "e" * 24)  # requires strength validation
    monkeypatch.setenv("APP_BASE_URL", "https://jarvis.example.com")  # required in production


# ---------------------------------------------------------------------------
# send_magic_link — token must never appear in logs or log_event context
# ---------------------------------------------------------------------------


class TestSendMagicLinkTokenNotLogged:
    """The raw link (bearer token) must NEVER appear in any log record."""

    async def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        *,
        pool: Any = None,
    ) -> list[dict[str, Any]]:
        """Run send_magic_link in dev-mode fallback; return captured log_event contexts."""
        import jarvis_common.email as email_mod

        # Force dev-mode path by making _dev_mode() return True
        monkeypatch.setattr(email_mod, "_dev_mode", lambda: True)

        captured_contexts: list[dict[str, Any]] = []

        async def fake_log_event(**kwargs: Any) -> None:
            captured_contexts.append(kwargs.get("context", {}))

        with patch("jarvis_common.email.log_event", side_effect=fake_log_event):
            with caplog.at_level(logging.DEBUG, logger="jarvis_common.email"):
                await email_mod.send_magic_link(_EMAIL, _LINK, pool=pool)

        return captured_contexts

    async def test_raw_link_not_in_caplog(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The raw magic-link URL must not appear in any log record."""
        await self._run(monkeypatch, caplog)

        for record in caplog.records:
            assert _LINK not in record.getMessage(), (
                f"Raw magic-link found in log record: {record.getMessage()!r}"
            )

    async def test_raw_token_not_in_caplog(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The raw token embedded in the link must not appear in any log record."""
        await self._run(monkeypatch, caplog)

        for record in caplog.records:
            assert _TOKEN not in record.getMessage(), (
                f"Raw token found in log record: {record.getMessage()!r}"
            )

    async def test_email_hash_in_caplog(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The SHA-256 hash of the email must appear in the log record."""
        await self._run(monkeypatch, caplog)

        expected_hash = _email_sha256(_EMAIL)
        assert any(expected_hash in record.getMessage() for record in caplog.records), (
            f"Expected email_hash {expected_hash!r} in log records; got: {[r.getMessage() for r in caplog.records]}"
        )

    async def test_link_issued_true_in_caplog(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """link_issued=true must appear in the log record."""
        await self._run(monkeypatch, caplog)

        assert any("link_issued=true" in record.getMessage() for record in caplog.records), (
            f"Expected 'link_issued=true' in log records; got: {[r.getMessage() for r in caplog.records]}"
        )

    async def test_raw_link_not_in_log_event_context(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """log_event context must not contain the raw link."""
        _conn = AsyncMock()
        _conn.fetch = AsyncMock(return_value=[])
        _conn.fetchrow = AsyncMock(return_value=None)
        fake_pool = AsyncMock()
        fake_pool.acquire.return_value.__aenter__.return_value = _conn
        contexts = await self._run(monkeypatch, caplog, pool=fake_pool)

        assert contexts, "Expected log_event to be called at least once"
        for ctx in contexts:
            ctx_str = str(ctx)
            assert _LINK not in ctx_str, f"Raw link found in log_event context: {ctx_str!r}"
            assert _TOKEN not in ctx_str, f"Raw token found in log_event context: {ctx_str!r}"

    async def test_log_event_context_contains_email_hash_and_link_issued(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """log_event context must contain email_hash and link_issued=True."""
        _conn = AsyncMock()
        _conn.fetch = AsyncMock(return_value=[])
        _conn.fetchrow = AsyncMock(return_value=None)
        fake_pool = AsyncMock()
        fake_pool.acquire.return_value.__aenter__.return_value = _conn
        contexts = await self._run(monkeypatch, caplog, pool=fake_pool)

        assert contexts, "Expected log_event to be called"
        ctx = contexts[0]
        assert ctx.get("email_hash") == _email_sha256(_EMAIL), (
            f"email_hash mismatch in log_event context: {ctx!r}"
        )
        assert ctx.get("link_issued") is True, f"link_issued not True in log_event context: {ctx!r}"

    async def test_raw_email_address_not_in_caplog(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The raw email address must not appear in any log record (only its hash)."""
        await self._run(monkeypatch, caplog)

        for record in caplog.records:
            assert _EMAIL not in record.getMessage(), (
                f"Raw email address found in log record: {record.getMessage()!r}"
            )


# ---------------------------------------------------------------------------
# SMTP deliverability gate — moved out of the env-only validate_production_config
# into the post-pool validate_runtime_config (multi-user PRODUCTION only).
# ---------------------------------------------------------------------------


def _runtime_pool(*, user_count: int, admin_count: int) -> MagicMock:
    """asyncpg-pool-shaped mock whose connection returns the given user counts."""
    pool, conn = make_pool_and_conn(with_transaction=False)
    conn.fetchval = AsyncMock(side_effect=[user_count, admin_count])
    return pool


class TestSmtpDeliverabilityGate:
    """The SMTP deliverability check is DB-aware and only evaluates a multi-user
    production box. When such a box has no deliverable relay it emits a startup
    WARNING rather than failing to boot, because an admin can still share manual
    sign-in links for non-owner users. A single-user owner box is never checked,
    and ``validate_production_config`` no longer gates SMTP at all (no false
    sync-gate crash on a DB-only-SMTP deployment)."""

    def test_validate_production_config_no_longer_gates_smtp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production + no SMTP env → ``validate_production_config`` must NOT raise
        (the env-only gate was removed; a DB-configured relay must not crash boot)."""
        _minimal_prod_env(monkeypatch)
        # No SMTP env vars set.
        validate_production_config()  # must not raise

    async def test_multi_user_production_no_smtp_warns_not_raises(self) -> None:
        """Production + >1 user + no deliverable relay → a startup WARNING, not a hard
        failure (admins can still share manual sign-in links when SMTP is absent)."""
        pool = _runtime_pool(user_count=2, admin_count=1)
        with (
            patch(
                "jarvis_common.email.effective_smtp_status",
                new=AsyncMock(return_value=(False, ["no relay configured"])),
            ),
            patch("jarvis_common.auth.logger") as mock_logger,
        ):
            await validate_runtime_config(
                pool,
                environment="production",
                setup_token_set=True,
                model_hmac_ok=True,
            )
        mock_logger.warning.assert_called_once()
        assert "SMTP" in mock_logger.warning.call_args.args[0]

    async def test_single_user_production_no_smtp_passes(self) -> None:
        """A single-user production box boots without SMTP (owner uses an API key)."""
        pool = _runtime_pool(user_count=1, admin_count=1)
        # SMTP branch is multi-user-only — effective_smtp_status is never consulted.
        await validate_runtime_config(
            pool,
            environment="production",
            setup_token_set=True,
            model_hmac_ok=True,
        )

    async def test_multi_user_production_deliverable_smtp_passes(self) -> None:
        """Production + >1 user + a deliverable relay → no error."""
        pool = _runtime_pool(user_count=2, admin_count=1)
        with patch(
            "jarvis_common.email.effective_smtp_status",
            new=AsyncMock(return_value=(True, [])),
        ):
            await validate_runtime_config(
                pool,
                environment="production",
                setup_token_set=True,
                model_hmac_ok=True,
            )

    def test_development_without_smtp_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-production boots are never gated on SMTP."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("DEV_MODE", "true")
        # No SMTP vars, no API key — all fine in dev.
        validate_production_config()  # must not raise


class TestFirstAdminSetupTokenGate:
    """A box with no admin yet and no setup token has an unprotected first-admin
    window. Production keeps failing to boot (the message is part of the contract);
    non-production only emits a startup WARNING so first-run ``docker compose up``
    is never broken."""

    _PROD_MESSAGE = (
        "JARVIS_SETUP_TOKEN must be set on a production deployment with no "
        "admin yet (prevents unauthenticated first-admin takeover)."
    )

    async def test_production_no_admin_no_token_still_raises(self) -> None:
        """Production behaviour is byte-identical: still raises the same message."""
        pool = _runtime_pool(user_count=0, admin_count=0)
        with pytest.raises(RuntimeError) as exc_info:
            await validate_runtime_config(
                pool,
                environment="production",
                setup_token_set=False,
                model_hmac_ok=True,
            )
        assert str(exc_info.value) == self._PROD_MESSAGE

    async def test_development_no_admin_no_token_warns_not_raises(self) -> None:
        """Non-production warns about the unprotected window and boots anyway."""
        pool = _runtime_pool(user_count=0, admin_count=0)
        with patch("jarvis_common.auth.logger") as mock_logger:
            await validate_runtime_config(
                pool,
                environment="development",
                setup_token_set=False,
                model_hmac_ok=True,
            )
        mock_logger.warning.assert_called_once()
        assert "unprotected" in mock_logger.warning.call_args.args[0]

    async def test_development_with_token_does_not_warn(self) -> None:
        """A configured setup token closes the window — no warning, no raise."""
        pool = _runtime_pool(user_count=0, admin_count=0)
        with patch("jarvis_common.auth.logger") as mock_logger:
            await validate_runtime_config(
                pool,
                environment="development",
                setup_token_set=True,
                model_hmac_ok=True,
            )
        mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# validate_production_config — LITELLM_MASTER_KEY / POSTGRES_PASSWORD
# strength, APP_BASE_URL required
# ---------------------------------------------------------------------------


def _prod_env_with_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fully valid production env so only the var under test can trip the gate."""
    _minimal_prod_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")


class TestValidateProductionConfigLitellmMasterKey:
    """LITELLM_MASTER_KEY must be present, strong, and not a placeholder."""

    def test_all_strong_prod_config_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Baseline: a fully-strong production config must not raise."""
        _prod_env_with_smtp(monkeypatch)
        validate_production_config()

    def test_missing_litellm_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

        with pytest.raises(RuntimeError, match="LITELLM_MASTER_KEY"):
            validate_production_config()

    def test_placeholder_litellm_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The literal dev placeholder from production-readiness-check.sh is rejected."""
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-jarvis-dev-test")

        with pytest.raises(RuntimeError, match="LITELLM_MASTER_KEY"):
            validate_production_config()

    def test_short_litellm_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-short")

        with pytest.raises(RuntimeError, match="LITELLM_MASTER_KEY"):
            validate_production_config()

    def test_substring_placeholder_litellm_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A long key containing a skeleton fragment ('changeme') is still rejected."""
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-CHANGEME-" + "z" * 30)

        with pytest.raises(RuntimeError, match="LITELLM_MASTER_KEY"):
            validate_production_config()

    # test_non_production_unaffected (LITELLM class copy) removed — merged into
    #   parametrized test_non_production_unaffected_parametrized below.


class TestValidateProductionConfigPostgresPassword:
    """POSTGRES_PASSWORD must be present, strong, and not a placeholder."""

    def test_missing_postgres_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            validate_production_config()

    def test_placeholder_postgres_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A long-enough but placeholder-derived password is still rejected.

        The denylist is the verbatim port of production-readiness-check.sh, so
        the 'changeme' skeleton fragment trips even at 20 chars.
        """
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("POSTGRES_PASSWORD", "changeme-prod-db-pwd")

        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            validate_production_config()

    def test_exact_placeholder_postgres_password_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exact-denylist value ('postgres') is rejected even though short
        (the placeholder check fires before the length check).
        """
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("POSTGRES_PASSWORD", "postgres")

        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            validate_production_config()

    def test_short_postgres_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("POSTGRES_PASSWORD", "short")

        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            validate_production_config()

    # test_non_production_unaffected (POSTGRES class copy) removed — merged into
    #   parametrized test_non_production_unaffected_parametrized below.


class TestValidateProductionConfigAppBaseUrl:
    """APP_BASE_URL must be set in production (host-poisoning guard)."""

    def test_missing_app_base_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.delenv("APP_BASE_URL", raising=False)

        with pytest.raises(RuntimeError, match="APP_BASE_URL"):
            validate_production_config()

    def test_blank_app_base_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace-only APP_BASE_URL is treated as unset."""
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("APP_BASE_URL", "   ")

        with pytest.raises(RuntimeError, match="APP_BASE_URL"):
            validate_production_config()

    def test_set_app_base_url_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("APP_BASE_URL", "https://jarvis.example.com")

        validate_production_config()

    # test_non_production_unaffected (APP_BASE_URL class copy) removed — merged into
    #   parametrized test_non_production_unaffected_parametrized below.


# ---------------------------------------------------------------------------
# three test_non_production_unaffected triplicates → one parametrized test
# (originally one copy per class: LITELLM gate, POSTGRES gate, APP_BASE_URL gate)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# validate_production_config — dev_cors_open gate
# ---------------------------------------------------------------------------


class TestValidateProductionConfigDevCorsOpen:
    """dev_cors_open=true must be rejected in ENVIRONMENT=production."""

    def test_dev_cors_open_in_production_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dev_cors_open=true with ENVIRONMENT=production must raise RuntimeError."""
        _minimal_prod_env(monkeypatch)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
        monkeypatch.setenv("DEV_CORS_OPEN", "true")

        with pytest.raises(RuntimeError, match="dev_cors_open"):
            validate_production_config()

    def test_dev_cors_open_false_in_production_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dev_cors_open=false (default) does not trip the gate in production."""
        _minimal_prod_env(monkeypatch)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
        monkeypatch.setenv("DEV_CORS_OPEN", "false")

        # Must not raise
        validate_production_config()

    def test_dev_cors_open_in_development_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dev_cors_open=true is allowed in non-production environments."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("DEV_MODE", "true")
        monkeypatch.setenv("DEV_CORS_OPEN", "true")

        # Must not raise
        validate_production_config()


@pytest.mark.parametrize(
    "env,dev_mode",
    [
        ("development", "true"),  # was TestValidateProductionConfigLitellmMasterKey
        ("staging", "true"),  # was TestValidateProductionConfigPostgresPassword
        ("development", "true"),  # was TestValidateProductionConfigAppBaseUrl (same env value)
    ],
    ids=["litellm-gate-dev", "postgres-gate-staging", "app-base-url-gate-dev"],
)
def test_non_production_unaffected_parametrized(
    env: str, dev_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-production environments are not subject to any validate_production_config gate.

    Replaces three identical test_non_production_unaffected methods that were
    copy-pasted across TestValidateProductionConfigLitellmMasterKey,
    TestValidateProductionConfigPostgresPassword, and TestValidateProductionConfigAppBaseUrl.
    All three original env-value pairs are preserved as parametrize cases.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("DEV_MODE", dev_mode)

    validate_production_config()


# ---------------------------------------------------------------------------
# CORS wildcard guard — fail-fast BEFORE middleware install
# ---------------------------------------------------------------------------


def test_cors_wildcard_blocked_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_CORS_OPEN", "false")

    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)

    with pytest.raises(RuntimeError, match="CORS wildcard"):
        configure_middleware_and_errors(app, limiter=limiter, cors_origins=["*"])

    from starlette.middleware.cors import CORSMiddleware

    middleware_classes = [m.cls for m in app.user_middleware]
    assert CORSMiddleware not in middleware_classes


def test_cors_wildcard_allowed_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_CORS_OPEN", "true")

    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)

    configure_middleware_and_errors(app, limiter=limiter, cors_origins=["*"])
