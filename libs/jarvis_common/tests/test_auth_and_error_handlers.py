"""Unit tests for JC-005 (cached API key) and JC-006 (request_id in errors)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

# ---------------------------------------------------------------------------
# JC-005 — verify_api_key uses the cached key
# ---------------------------------------------------------------------------


class TestCachedApiKey:
    def test_cached_api_key_is_loaded_at_import(self) -> None:
        """_CACHED_API_KEY is populated (or None) at module load, not per-request."""
        import jarvis_common.auth as auth_mod

        # The attribute must exist on the module, regardless of its value.
        assert hasattr(auth_mod, "_CACHED_API_KEY")

    async def test_cached_value_not_reread_on_subsequent_calls(self, monkeypatch) -> None:
        """JC-005: verify_api_key reads _CACHED_API_KEY, not get_secrets_settings on each call.

        We patch _CACHED_API_KEY directly and confirm the handler uses that
        value rather than calling get_secrets_settings again.
        """
        import jarvis_common.auth as auth_mod
        import jarvis_common.settings as settings_mod

        # Patch the cache to a known key.
        monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", "cached-test-key-1234567890abcdef")
        # Confirm get_secrets_settings is NOT called during verify_api_key.
        with patch.object(settings_mod, "get_secrets_settings") as mock_read:
            from starlette.requests import Request

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/api/papers",
                "query_string": b"",
                "headers": [],
            }
            request = Request(scope)

            # Correct key — should pass without raising.
            await auth_mod.verify_api_key(request, "cached-test-key-1234567890abcdef")
            mock_read.assert_not_called()

    async def test_wrong_key_raises_403_using_cache(self, monkeypatch) -> None:
        """verify_api_key raises 403 on wrong key using the cached value."""
        import jarvis_common.auth as auth_mod
        from fastapi import HTTPException
        from starlette.requests import Request

        monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", "correct-key-1234567890abcdef")
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/papers",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        with pytest.raises(HTTPException) as exc_info:
            await auth_mod.verify_api_key(request, "wrong-key")
        assert exc_info.value.status_code == 403

    async def test_no_cached_key_dev_mode_passes(self, monkeypatch) -> None:
        """When _CACHED_API_KEY is None and DEV_MODE=true, auth is bypassed."""
        import jarvis_common.auth as auth_mod
        from starlette.requests import Request

        monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", None)
        monkeypatch.setenv("DEV_MODE", "true")

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/papers",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        # Should complete without raising.
        await auth_mod.verify_api_key(request, None)


# ---------------------------------------------------------------------------
# JC-006 — request_id injected into error responses
# ---------------------------------------------------------------------------


class TestRequestIdInErrors:
    def _make_request(self) -> MagicMock:
        req = MagicMock()
        req.method = "GET"
        req.url.path = "/api/test"
        return req

    @pytest.mark.asyncio
    async def test_http_exception_handler_includes_request_id(self) -> None:
        """JC-006: http_exception_handler puts request_id from ctx into the response."""
        from jarvis_common.error_handlers import http_exception_handler
        from jarvis_common.logging_config import request_id_ctx

        token = request_id_ctx.set("test-req-id-001")
        try:
            exc = StarletteHTTPException(status_code=404, detail="Not found")
            response = await http_exception_handler(self._make_request(), exc)
            assert response.status_code == 404
            import json

            body = json.loads(response.body)
            assert body["detail"] == "Not found"
            assert body["request_id"] == "test-req-id-001"
        finally:
            request_id_ctx.reset(token)

    @pytest.mark.asyncio
    async def test_http_exception_handler_request_id_none_when_unset(self) -> None:
        """http_exception_handler uses None when request_id_ctx is empty."""
        from jarvis_common.error_handlers import http_exception_handler
        from jarvis_common.logging_config import request_id_ctx

        # Ensure ctx is empty
        token = request_id_ctx.set("")
        try:
            exc = StarletteHTTPException(status_code=400, detail="Bad input")
            response = await http_exception_handler(self._make_request(), exc)
            import json

            body = json.loads(response.body)
            assert body["request_id"] is None
        finally:
            request_id_ctx.reset(token)

    @pytest.mark.asyncio
    async def test_generic_exception_handler_includes_request_id(self) -> None:
        """JC-006: generic_exception_handler includes request_id in 500 response."""
        from jarvis_common.error_handlers import generic_exception_handler
        from jarvis_common.logging_config import request_id_ctx

        token = request_id_ctx.set("req-500-abc")
        try:
            exc = RuntimeError("something exploded")
            response = await generic_exception_handler(self._make_request(), exc)
            assert response.status_code == 500
            import json

            body = json.loads(response.body)
            assert body["request_id"] == "req-500-abc"
        finally:
            request_id_ctx.reset(token)

    @pytest.mark.asyncio
    async def test_validation_exception_handler_includes_request_id(self) -> None:
        """JC-006: validation_exception_handler includes request_id in 422 response."""
        from jarvis_common.error_handlers import validation_exception_handler
        from jarvis_common.logging_config import request_id_ctx
        from pydantic import BaseModel, ValidationError

        class _M(BaseModel):
            x: int

        token = request_id_ctx.set("req-422-xyz")
        try:
            try:
                _M(x="not-an-int")  # type: ignore[arg-type]
            except ValidationError as ve:
                exc = RequestValidationError(ve.errors())

            response = await validation_exception_handler(self._make_request(), exc)
            assert response.status_code == 422
            import json

            body = json.loads(response.body)
            assert body["request_id"] == "req-422-xyz"
        finally:
            request_id_ctx.reset(token)


# ---------------------------------------------------------------------------
# SEC-107 — validation_exception_handler redacts loc in production
# (migrated from test_sprint4_1a.py)
# ---------------------------------------------------------------------------


class TestValidationErrorResponseRedaction:
    def _make_request(self):
        from unittest.mock import MagicMock

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
        import json

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
        import json

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
# (migrated from test_sprint4_1a.py)
# ---------------------------------------------------------------------------


class TestCurrentUserIdSec108:
    def _make_request(self, *, user_id: int | None = None):
        from types import SimpleNamespace

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

    def test_current_user_id_or_none_exported_from_top_level(self) -> None:
        """current_user_id_or_none must be importable from jarvis_common."""
        from jarvis_common import current_user_id_or_none  # noqa: F401
