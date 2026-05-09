"""Tests for Sprint 4 Wave 1 Batch 1A fixes.

Covers:
- JC-005: validated_model_with_reason surfaces fallback reason
- SEC-107: validation_exception_handler redacts loc in production
- SEC-108: current_user_id_or_none still returns None; assert_multi_tenant raises
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# JC-005 — validated_model_with_reason surfaces fallback reason
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


# ---------------------------------------------------------------------------
# SEC-107 — validation_exception_handler redacts loc in production
# ---------------------------------------------------------------------------


class TestValidationErrorResponseRedaction:
    def _make_request(self) -> MagicMock:
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/api/test"
        return req

    def _make_validation_exc(self):  # returns RequestValidationError
        from fastapi.exceptions import RequestValidationError
        from pydantic import BaseModel, ValidationError

        class _M(BaseModel):
            secret_field: int

        try:
            _M(secret_field="not-an-int")  # type: ignore[arg-type]
        except ValidationError as ve:
            return RequestValidationError(ve.errors())
        raise AssertionError("Expected ValidationError was not raised")

    @pytest.mark.asyncio
    async def test_validation_error_response_redacts_loc_in_production(self, monkeypatch) -> None:
        """SEC-107: In production (DEV_MODE=false), response body must NOT contain 'errors'."""
        import jarvis_common.error_handlers as eh

        monkeypatch.setenv("DEV_MODE", "false")
        exc = self._make_validation_exc()
        response = await eh.validation_exception_handler(self._make_request(), exc)
        body = json.loads(response.body)

        assert response.status_code == 422
        assert body["detail"] == "Validation error"
        # 'errors' key must be absent in production
        assert "errors" not in body

    @pytest.mark.asyncio
    async def test_validation_error_response_includes_errors_in_dev(self, monkeypatch) -> None:
        """In DEV_MODE=true, the 'errors' key should be present for debugging."""
        import jarvis_common.error_handlers as eh

        monkeypatch.setenv("DEV_MODE", "true")
        exc = self._make_validation_exc()
        response = await eh.validation_exception_handler(self._make_request(), exc)
        body = json.loads(response.body)

        assert response.status_code == 422
        assert body["detail"] == "Validation error"
        assert "errors" in body

    @pytest.mark.asyncio
    async def test_production_handler_logs_full_errors(self, monkeypatch, caplog) -> None:
        """SEC-107: server-side log must contain field error details in production."""
        import logging

        import jarvis_common.error_handlers as eh

        monkeypatch.setenv("DEV_MODE", "false")
        exc = self._make_validation_exc()

        with caplog.at_level(logging.WARNING, logger="jarvis_common.error_handlers"):
            await eh.validation_exception_handler(self._make_request(), exc)

        # The warning should contain something from the pydantic errors list
        assert any(
            "secret_field" in record.message or "Validation error" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# SEC-108 — current_user_id_or_none still returns None; guard raises
# ---------------------------------------------------------------------------


class TestCurrentUserIdSec108:
    def _make_request(self, *, user_id: int | None = None) -> SimpleNamespace:
        # Phase 2 WS-2A: helpers now read request.state.user_id populated by
        # the session middleware, so the test fixture builds a real
        # SimpleNamespace with the desired state instead of a MagicMock
        # (whose attribute auto-vivification returns a Mock, not None).
        state = SimpleNamespace()
        if user_id is not None:
            state.user_id = user_id
        return SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/test"), state=state)

    @pytest.mark.asyncio
    async def test_current_user_id_or_none_returns_none_without_session(self) -> None:
        """current_user_id_or_none returns None when no session middleware ran."""
        from jarvis_common.auth import current_user_id_or_none

        result = await current_user_id_or_none(self._make_request())
        assert result is None

    @pytest.mark.asyncio
    async def test_current_user_id_returns_none_without_session(self) -> None:
        """current_user_id returns None when no session middleware ran."""
        from jarvis_common.auth import current_user_id

        result = await current_user_id(self._make_request())
        assert result is None

    @pytest.mark.asyncio
    async def test_current_user_id_or_none_returns_state_value_when_session_present(self) -> None:
        """WS-2A contract: helpers expose request.state.user_id when middleware set it."""
        from jarvis_common.auth import current_user_id_or_none

        result = await current_user_id_or_none(self._make_request(user_id=42))
        assert result == 42

    def test_assert_multi_tenant_not_implemented_raises(self) -> None:
        """Guard still raises — semantics tightened from "not implemented" to "no session"."""
        from jarvis_common.auth import assert_multi_tenant_not_implemented

        with pytest.raises(NotImplementedError):
            assert_multi_tenant_not_implemented()

    def test_assert_multi_tenant_guard_exported_from_top_level(self) -> None:
        """assert_multi_tenant_not_implemented must be importable from jarvis_common."""
        from jarvis_common import assert_multi_tenant_not_implemented  # noqa: F401

    def test_current_user_id_or_none_exported_from_top_level(self) -> None:
        """current_user_id_or_none must be importable from jarvis_common."""
        from jarvis_common import current_user_id_or_none  # noqa: F401


# ---------------------------------------------------------------------------
# DRY-003 — crypto re-exports from jarvis_common top-level
# ---------------------------------------------------------------------------


class TestCryptoReexports:
    def test_encrypt_secret_importable(self) -> None:
        from jarvis_common import encrypt_secret  # noqa: F401

    def test_decrypt_secret_importable(self) -> None:
        from jarvis_common import decrypt_secret  # noqa: F401

    def test_mask_secret_importable(self) -> None:
        from jarvis_common import mask_secret  # noqa: F401

    def test_refresh_fernet_cache_importable(self) -> None:
        from jarvis_common import refresh_fernet_cache  # noqa: F401

    def test_validate_encrypted_config_rows_importable(self) -> None:
        from jarvis_common import validate_encrypted_config_rows  # noqa: F401

    def test_mask_secret_functional(self) -> None:
        """Smoke-test that the re-exported mask_secret actually works."""
        from jarvis_common import mask_secret

        assert mask_secret("") == ""
        assert mask_secret("abc") == "****"
        # H.1: mask_secret now masks the prefix and shows last 4 chars
        # (was first 4 + "****" — leaked provider prefixes like "sk-ant-")
        assert mask_secret("abcde") == "****bcde"
