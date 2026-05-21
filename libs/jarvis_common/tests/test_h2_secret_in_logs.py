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
from unittest.mock import AsyncMock, patch

import pytest
from jarvis_common.auth import validate_production_config

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
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-" + "d" * 40)  # SEC-A
    monkeypatch.setenv("POSTGRES_PASSWORD", "e" * 24)  # SEC-A
    monkeypatch.setenv("APP_BASE_URL", "https://jarvis.example.com")  # SEC-B


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
# validate_production_config — SMTP gate
# ---------------------------------------------------------------------------


class TestValidateProductionConfigSmtpGate:
    """SMTP fields must be required when ENVIRONMENT=production and DEV_SMTP_LOG_ONLY=false."""

    def test_production_no_smtp_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """production + no SMTP → RuntimeError mentioning missing SMTP fields."""
        _minimal_prod_env(monkeypatch)
        # No SMTP env vars set

        with pytest.raises(RuntimeError, match="SMTP"):
            validate_production_config()

    def test_production_partial_smtp_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """production + only SMTP_HOST set (missing PORT + FROM) → RuntimeError."""
        _minimal_prod_env(monkeypatch)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        # SMTP_PORT and SMTP_FROM missing

        with pytest.raises(RuntimeError, match="SMTP"):
            validate_production_config()

    def test_production_with_full_smtp_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """production + SMTP_HOST + SMTP_PORT + SMTP_FROM → no error."""
        _minimal_prod_env(monkeypatch)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("SMTP_FROM", "noreply@example.com")

        # Must not raise
        validate_production_config()

    def test_development_without_smtp_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-production environments are not affected by the SMTP gate."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("DEV_MODE", "true")
        # No SMTP vars, no API key — all fine in dev

        # Must not raise
        validate_production_config()

    def test_staging_without_smtp_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """staging environment is not subject to the SMTP gate."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "staging")
        monkeypatch.setenv("DEV_MODE", "true")

        # Must not raise
        validate_production_config()

    def test_production_smtp_error_message_names_missing_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Error message must name the specific missing fields."""
        _minimal_prod_env(monkeypatch)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        # SMTP_PORT and SMTP_FROM missing

        with pytest.raises(RuntimeError) as exc_info:
            validate_production_config()

        msg = str(exc_info.value)
        assert "SMTP_PORT" in msg, f"Expected SMTP_PORT in error message: {msg!r}"
        assert "SMTP_FROM" in msg, f"Expected SMTP_FROM in error message: {msg!r}"
        # SMTP_HOST was set — it must not appear in the "(missing: ...)" section.
        # The static hint text may still reference the field names, so we extract
        # just the parenthesised missing-fields fragment for this check.
        import re

        missing_section = re.search(r"\(missing: ([^)]+)\)", msg)
        assert missing_section is not None, f"Expected '(missing: ...)' in message: {msg!r}"
        missing_fields = missing_section.group(1)
        assert "SMTP_HOST" not in missing_fields, (
            f"SMTP_HOST should not be listed as missing: {missing_fields!r}"
        )


# ---------------------------------------------------------------------------
# validate_production_config — SEC-A: LITELLM_MASTER_KEY / POSTGRES_PASSWORD
# strength, SEC-B: APP_BASE_URL required
# ---------------------------------------------------------------------------


def _prod_env_with_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fully valid production env so only the var under test can trip the gate."""
    _minimal_prod_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")


class TestValidateProductionConfigLitellmMasterKey:
    """SEC-A — LITELLM_MASTER_KEY must be present, strong, and not a placeholder."""

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

    # B2-12: test_non_production_unaffected (LITELLM class copy) removed — merged into
    #   parametrized test_non_production_unaffected_parametrized below.


class TestValidateProductionConfigPostgresPassword:
    """SEC-A — POSTGRES_PASSWORD must be present, strong, and not a placeholder."""

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
        (the placeholder check fires before the length check)."""
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("POSTGRES_PASSWORD", "postgres")

        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            validate_production_config()

    def test_short_postgres_password_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prod_env_with_smtp(monkeypatch)
        monkeypatch.setenv("POSTGRES_PASSWORD", "short")

        with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
            validate_production_config()

    # B2-12: test_non_production_unaffected (POSTGRES class copy) removed — merged into
    #   parametrized test_non_production_unaffected_parametrized below.


class TestValidateProductionConfigAppBaseUrl:
    """SEC-B — APP_BASE_URL must be set in production (host-poisoning guard)."""

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

    # B2-12: test_non_production_unaffected (APP_BASE_URL class copy) removed — merged into
    #   parametrized test_non_production_unaffected_parametrized below.


# ---------------------------------------------------------------------------
# B2-12: three test_non_production_unaffected triplicates → one parametrized test
# (originally one copy per class: LITELLM gate, POSTGRES gate, APP_BASE_URL gate)
# ---------------------------------------------------------------------------


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
