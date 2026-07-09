"""Unit tests for paper_ingestion.routers.auth — magic-link cooldown.

Tests that a second call to request_link within MAGIC_LINK_COOLDOWN (2 minutes)
skips the INSERT and still returns sent=True, so exactly one row is inserted
even when the same email is requested twice in quick succession.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_limiter():
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


def _build_request(pool: MagicMock, url_path: str = "/api/auth/request-link") -> SimpleNamespace:
    """Build a minimal Request stub for auth router tests."""
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    url = SimpleNamespace(
        path=url_path,
        replace=lambda **kw: "http://test/auth/verify?token=t",
    )
    return SimpleNamespace(
        url=url,
        app=app,
        client=SimpleNamespace(host="127.0.0.1"),
        cookies={},
        state=SimpleNamespace(),
    )


# ---------------------------------------------------------------------------
# magic-link cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_link_cooldown_skips_second_insert() -> None:
    """Two consecutive request_link calls produce exactly ONE magic_link_tokens INSERT.

    The sequence:
    1. First call: fetchval returns None (no recent token) → INSERT happens.
    2. Second call within cooldown window: fetchval returns a recent datetime
       → INSERT skipped, but sent=True still returned.
    """
    from paper_ingestion.routers.auth import RequestLinkBody, RequestLinkResponse, request_link

    user_row = {"id": 42}
    # conn.fetchrow always returns the user row (known email).
    # conn.fetchval returns None on first call (no recent token), then a
    # just-now datetime on the second call (within cooldown).
    now = datetime.now(UTC)
    recent_ts = now - timedelta(seconds=30)  # 30 s ago — within 2-min cooldown

    pool, conn = make_pool_and_conn(
        fetchrow_return=user_row,
    )
    # Override fetchval to use side_effect for sequential calls.
    # Call 1 (first request): None — no existing token.
    # Call 2 (second request): recent_ts — within cooldown.
    conn.fetchval = AsyncMock(side_effect=[None, recent_ts])

    execute_calls: list = []

    async def _tracked_execute(query, *args):
        execute_calls.append((query, args))

    conn.execute = AsyncMock(side_effect=_tracked_execute)

    request = _build_request(pool)
    body = RequestLinkBody(email="cooldown@example.com")

    with patch("paper_ingestion.routers.auth.send_magic_link", AsyncMock()):
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            # First call — should INSERT.
            resp1 = await request_link(body=body, request=request)
            # Second call — should skip INSERT.
            resp2 = await request_link(body=body, request=request)

    assert isinstance(resp1, RequestLinkResponse)
    assert resp1.sent is True

    assert isinstance(resp2, RequestLinkResponse)
    assert resp2.sent is True

    # Exactly one INSERT into magic_link_tokens (the second call was suppressed).
    insert_calls = [c for c in execute_calls if "INSERT INTO magic_link_tokens" in c[0]]
    assert len(insert_calls) == 1, (
        f"Expected exactly 1 INSERT, got {len(insert_calls)}: {insert_calls}"
    )


@pytest.mark.asyncio
async def test_request_link_cooldown_logs_info_on_suppression() -> None:
    """Second request within cooldown logs auth_request_link_cooldown at INFO."""
    from paper_ingestion.routers.auth import RequestLinkBody, request_link

    user_row = {"id": 42}
    now = datetime.now(UTC)
    recent_ts = now - timedelta(seconds=30)

    pool, conn = make_pool_and_conn(
        fetchrow_return=user_row,
    )
    conn.fetchval = AsyncMock(side_effect=[None, recent_ts])
    conn.execute = AsyncMock(return_value=None)

    request = _build_request(pool)
    body = RequestLinkBody(email="logtest@example.com")

    with patch("paper_ingestion.routers.auth.send_magic_link", AsyncMock()):
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            with patch(
                "paper_ingestion.routers.auth.logger",
            ) as mock_logger:
                # First call — normal INSERT path.
                await request_link(body=body, request=request)
                # Second call — cooldown path should log.
                await request_link(body=body, request=request)

    # Verify logger.info was called with the cooldown message at least once.
    info_calls = [
        call
        for call in mock_logger.info.call_args_list
        if "auth_request_link_cooldown" in str(call)
    ]
    assert len(info_calls) >= 1, (
        f"Expected auth_request_link_cooldown info log, got: {mock_logger.info.call_args_list}"
    )


# ---------------------------------------------------------------------------
# enumeration resistance + swallowed-send event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_link_unknown_email_sent_true_no_insert() -> None:
    """Unknown email → {sent: true}, NO token INSERT, decoy hash + second acquire.

    Enumeration invariant: the response is identical to the known-email path and
    the branch performs the decoy CPU work + an equivalent ``pool.acquire`` so
    there is no order-of-magnitude timing split. No send-failure event is
    emitted (an unknown email is not a failure — an event would be a per-account
    signal).
    """
    from paper_ingestion.routers.auth import RequestLinkBody, RequestLinkResponse, request_link

    pool, conn = make_pool_and_conn(fetchrow_return=None)  # unknown email
    conn.execute = AsyncMock()

    request = _build_request(pool)
    body = RequestLinkBody(email="ghost@example.com")

    with patch("paper_ingestion.routers.auth.send_magic_link", AsyncMock()) as send_mock:
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            with patch("paper_ingestion.routers.auth.log_event", AsyncMock()) as event_mock:
                resp = await request_link(body=body, request=request)

    assert isinstance(resp, RequestLinkResponse)
    assert resp.sent is True
    # Unknown email performs no DB write — the token INSERT never runs.
    conn.execute.assert_not_awaited()
    # No send attempt and therefore no send-failure event.
    send_mock.assert_not_awaited()
    event_mock.assert_not_awaited()
    # Decoy: the branch performs a second pool.acquire (mirrors the known branch).
    assert pool.acquire.call_count == 2


@pytest.mark.asyncio
async def test_request_link_send_failure_writes_event_and_returns_sent() -> None:
    """A swallowed send failure writes exactly ONE auth/magic_link_send_failed
    event and STILL returns {sent: true} (enumeration/timing defense intact)."""
    from paper_ingestion.routers.auth import RequestLinkBody, RequestLinkResponse, request_link

    pool, conn = make_pool_and_conn(fetchrow_return={"id": 7}, fetchval_return=None)
    conn.execute = AsyncMock(return_value=None)

    request = _build_request(pool)
    body = RequestLinkBody(email="relaydown@example.com")

    with patch(
        "paper_ingestion.routers.auth.send_magic_link",
        AsyncMock(side_effect=RuntimeError("smtp down")),
    ):
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            with patch("paper_ingestion.routers.auth.log_event", AsyncMock()) as event_mock:
                resp = await request_link(body=body, request=request)

    assert isinstance(resp, RequestLinkResponse)
    assert resp.sent is True
    event_mock.assert_awaited_once()
    kwargs = event_mock.await_args.kwargs
    assert kwargs["level"] == "warning"
    assert kwargs["category"] == "auth"
    assert kwargs["source"] == "auth"
    assert kwargs["message"] == "magic_link_send_failed"
    # PII-free: only a SHA-256 email hash, never the raw email.
    assert "email_hash" in kwargs["context"]
    assert "relaydown@example.com" not in str(kwargs["context"])


@pytest.mark.asyncio
async def test_request_link_cooldown_writes_no_send_failed_event() -> None:
    """The cooldown early-return writes NO magic_link_send_failed event (it is
    not a failure) and still returns {sent: true}."""
    from paper_ingestion.routers.auth import RequestLinkBody, request_link

    recent_ts = datetime.now(UTC) - timedelta(seconds=30)  # within 2-min cooldown
    pool, conn = make_pool_and_conn(fetchrow_return={"id": 7}, fetchval_return=recent_ts)
    conn.execute = AsyncMock(return_value=None)

    request = _build_request(pool)
    body = RequestLinkBody(email="cool@example.com")

    with patch("paper_ingestion.routers.auth.send_magic_link", AsyncMock()) as send_mock:
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            with patch("paper_ingestion.routers.auth.log_event", AsyncMock()) as event_mock:
                resp = await request_link(body=body, request=request)

    assert resp.sent is True
    send_mock.assert_not_awaited()
    event_mock.assert_not_awaited()
