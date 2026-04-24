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

    def test_cached_value_not_reread_on_subsequent_calls(self, monkeypatch) -> None:
        """JC-005: verify_api_key reads _CACHED_API_KEY, not read_secret on each call.

        We patch _CACHED_API_KEY directly and confirm the handler uses that
        value rather than calling read_secret again.
        """
        import jarvis_common.auth as auth_mod

        # Patch the cache to a known key.
        monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", "cached-test-key-1234567890abcdef")
        # Confirm read_secret is NOT called during verify_api_key.
        with patch.object(auth_mod, "read_secret") as mock_read:
            # Run verify_api_key synchronously via asyncio.
            import asyncio

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
            asyncio.get_event_loop().run_until_complete(
                auth_mod.verify_api_key(request, "cached-test-key-1234567890abcdef")
            )
            mock_read.assert_not_called()

    def test_wrong_key_raises_403_using_cache(self, monkeypatch) -> None:
        """verify_api_key raises 403 on wrong key using the cached value."""
        import asyncio

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
            asyncio.get_event_loop().run_until_complete(
                auth_mod.verify_api_key(request, "wrong-key")
            )
        assert exc_info.value.status_code == 403

    def test_no_cached_key_dev_mode_passes(self, monkeypatch) -> None:
        """When _CACHED_API_KEY is None and DEV_MODE=true, auth is bypassed."""
        import asyncio

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
        asyncio.get_event_loop().run_until_complete(auth_mod.verify_api_key(request, None))


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
