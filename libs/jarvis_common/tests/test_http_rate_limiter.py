"""Tests for jarvis_common.http_rate_limiter."""

from __future__ import annotations

import pytest
from jarvis_common.http_rate_limiter import rate_limit_exceeded_handler
from limits import parse
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit


@pytest.mark.asyncio
async def test_rate_limit_exceeded_handler_includes_retry_after_header_60s():
    """429 response must include Retry-After header with reset time (60s for minute granularity)."""
    # Create a RateLimitItem from a limit string with minute granularity
    rate_limit_item = parse("5/minute")

    # Create a Limit wrapper (what slowapi passes to the exception)
    limit = Limit(
        limit=rate_limit_item,
        key_func=get_remote_address,
        scope=None,
        per_method=False,
        methods=None,
        error_message=None,
        exempt_when=None,
        cost=1,
        override_defaults=False,
    )

    # Create the RateLimitExceeded exception
    exc = RateLimitExceeded(limit)

    # Mock request object (handler doesn't use it but signature requires it)
    class MockRequest:
        pass

    # Call the handler
    response = await rate_limit_exceeded_handler(MockRequest(), exc)

    # Verify status code
    assert response.status_code == 429

    # Verify Retry-After header with 60 seconds for minute granularity
    assert "Retry-After" in response.headers
    assert response.headers["Retry-After"] == "60"

    # Verify response body contains error detail
    assert "Rate limit exceeded" in response.body.decode()


@pytest.mark.asyncio
async def test_rate_limit_exceeded_handler_includes_retry_after_header_second():
    """429 response must include Retry-After header with reset time (1s for second granularity)."""
    # Create a RateLimitItem from a limit string with second granularity
    rate_limit_item = parse("100/second")

    # Create a Limit wrapper
    limit = Limit(
        limit=rate_limit_item,
        key_func=get_remote_address,
        scope=None,
        per_method=False,
        methods=None,
        error_message=None,
        exempt_when=None,
        cost=1,
        override_defaults=False,
    )

    # Create the RateLimitExceeded exception
    exc = RateLimitExceeded(limit)

    # Mock request object
    class MockRequest:
        pass

    # Call the handler
    response = await rate_limit_exceeded_handler(MockRequest(), exc)

    # Verify status code
    assert response.status_code == 429

    # Verify Retry-After header with 1 second for second granularity
    assert "Retry-After" in response.headers
    assert response.headers["Retry-After"] == "1"

    # Verify response body contains error detail
    assert "Rate limit exceeded" in response.body.decode()


@pytest.mark.asyncio
async def test_rate_limit_exceeded_handler_includes_retry_after_header_hour():
    """429 response must include Retry-After header with reset time (3600s for hour granularity)."""
    # Create a RateLimitItem from a limit string with hour granularity
    rate_limit_item = parse("1000/hour")

    # Create a Limit wrapper
    limit = Limit(
        limit=rate_limit_item,
        key_func=get_remote_address,
        scope=None,
        per_method=False,
        methods=None,
        error_message=None,
        exempt_when=None,
        cost=1,
        override_defaults=False,
    )

    # Create the RateLimitExceeded exception
    exc = RateLimitExceeded(limit)

    # Mock request object
    class MockRequest:
        pass

    # Call the handler
    response = await rate_limit_exceeded_handler(MockRequest(), exc)

    # Verify status code
    assert response.status_code == 429

    # Verify Retry-After header with 3600 seconds for hour granularity
    assert "Retry-After" in response.headers
    assert response.headers["Retry-After"] == "3600"

    # Verify response body contains error detail
    assert "Rate limit exceeded" in response.body.decode()


@pytest.mark.asyncio
async def test_rate_limit_exceeded_handler_fallback_on_malformed_limit():
    """429 response uses 60s fallback if limit object structure is unexpected."""

    # Create a mock exception with a malformed/None limit
    class MalformedLimitException(RateLimitExceeded):
        def __init__(self):
            self.limit = None

    exc = MalformedLimitException()

    # Mock request object
    class MockRequest:
        pass

    # Call the handler - should not raise, should fall back to 60s
    response = await rate_limit_exceeded_handler(MockRequest(), exc)

    # Verify status code
    assert response.status_code == 429

    # Verify Retry-After header defaults to 60
    assert "Retry-After" in response.headers
    assert response.headers["Retry-After"] == "60"
