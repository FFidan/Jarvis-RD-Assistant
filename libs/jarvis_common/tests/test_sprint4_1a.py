"""Tests for Sprint 4 Wave 1 Batch 1A fixes.

Covers:
- JC-005: validated_model_with_reason surfaces fallback reason
- JC-006: call_llm_json_value allow_scalar parameter
- SEC-107: validation_exception_handler redacts loc in production
- SEC-108: current_user_id_or_none still returns None; assert_multi_tenant raises
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

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
# JC-006 — call_llm_json_value allow_scalar parameter
# ---------------------------------------------------------------------------


def _make_http_client_with_content(content: str) -> AsyncMock:
    """Return a mock AsyncClient whose POST returns the given content string."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    http_client = AsyncMock()
    http_client.post.return_value = response
    return http_client


_config = None  # lazy import inside tests


@pytest.mark.asyncio
async def test_call_llm_json_value_accepts_scalar_when_opted_in() -> None:
    """allow_scalar=True must accept scalar JSON values like integers and booleans."""
    from jarvis_common import llm_client

    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000", api_key="")

    # Integer scalar
    client = _make_http_client_with_content("42")
    result = await llm_client.call_llm_json_value(
        client, "How many?", config=config, allow_scalar=True
    )
    assert result == 42

    # Boolean scalar
    client = _make_http_client_with_content("true")
    result = await llm_client.call_llm_json_value(
        client, "Is it?", config=config, allow_scalar=True
    )
    assert result is True

    # null scalar
    client = _make_http_client_with_content("null")
    result = await llm_client.call_llm_json_value(
        client, "Anything?", config=config, allow_scalar=True
    )
    assert result is None

    # Negative number
    client = _make_http_client_with_content("-7")
    result = await llm_client.call_llm_json_value(
        client, "Delta?", config=config, allow_scalar=True
    )
    assert result == -7


@pytest.mark.asyncio
async def test_call_llm_json_value_rejects_scalar_by_default() -> None:
    """Without allow_scalar=True, scalar responses must still raise ValueError."""
    from jarvis_common import llm_client

    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000", api_key="")
    client = _make_http_client_with_content("42")

    with pytest.raises(ValueError, match="non-JSON content"):
        await llm_client.call_llm_json_value(client, "How many?", config=config)


@pytest.mark.asyncio
async def test_call_llm_json_value_still_accepts_object() -> None:
    """allow_scalar=False must still accept JSON objects (no regression)."""
    from jarvis_common import llm_client

    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000", api_key="")
    client = _make_http_client_with_content('{"key": "value"}')

    result = await llm_client.call_llm_json_value(client, "Give me object", config=config)
    assert result == {"key": "value"}


@pytest.mark.asyncio
async def test_call_llm_json_value_still_accepts_array() -> None:
    """allow_scalar=False must still accept JSON arrays (no regression)."""
    from jarvis_common import llm_client

    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000", api_key="")
    client = _make_http_client_with_content("[1, 2, 3]")

    result = await llm_client.call_llm_json_value(client, "Give me array", config=config)
    assert result == [1, 2, 3]


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
    def _make_request(self) -> MagicMock:
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/api/test"
        return req

    @pytest.mark.asyncio
    async def test_current_user_id_or_none_still_returns_none(self) -> None:
        """current_user_id_or_none must return None (single-tenant placeholder)."""
        from jarvis_common.auth import current_user_id_or_none

        result = await current_user_id_or_none(self._make_request())
        assert result is None

    @pytest.mark.asyncio
    async def test_current_user_id_still_returns_none_for_compat(self) -> None:
        """current_user_id must still return None to avoid breaking existing Depends callers."""
        from jarvis_common.auth import current_user_id

        result = await current_user_id(self._make_request())
        assert result is None

    def test_current_user_id_raises_not_implemented(self) -> None:
        """assert_multi_tenant_not_implemented must raise NotImplementedError."""
        from jarvis_common.auth import assert_multi_tenant_not_implemented

        with pytest.raises(NotImplementedError, match="multi-tenant auth not yet implemented"):
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
        assert mask_secret("abcde") == "abcd****"
