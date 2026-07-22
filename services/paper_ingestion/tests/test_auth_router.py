"""Unit tests for paper_ingestion.routers.auth — magic-link cooldown.

Tests that a second call to request_link within MAGIC_LINK_COOLDOWN (2 minutes)
skips the INSERT and still returns sent=True, so exactly one row is inserted
even when the same email is requested twice in quick succession.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
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
        replace=lambda **kw: "http://test/auth/verify",
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
            resp1 = await request_link(
                body=body,
                request=request,
                background_tasks=BackgroundTasks(),
            )
            # Second call — should skip INSERT.
            resp2 = await request_link(
                body=body,
                request=request,
                background_tasks=BackgroundTasks(),
            )

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
                await request_link(
                    body=body,
                    request=request,
                    background_tasks=BackgroundTasks(),
                )
                # Second call — cooldown path should log.
                await request_link(
                    body=body,
                    request=request,
                    background_tasks=BackgroundTasks(),
                )

    # Verify logger.info was called with the cooldown message at least once.
    info_calls = [
        call
        for call in mock_logger.info.call_args_list
        if "auth_request_link_cooldown" in str(call)
    ]
    assert len(info_calls) >= 1, (
        f"Expected auth_request_link_cooldown info log, got: {mock_logger.info.call_args_list}"
    )


@pytest.mark.asyncio
async def test_request_link_cooldown_probe_is_login_scoped() -> None:
    """The cooldown probe SELECT is scoped to login tokens (``pending_email IS NULL``).

    An unscoped probe also matches in-flight email-change tokens
    (``pending_email`` set), so an email change within the 2-min window would
    suppress login-link issuance. The probe must scope to login tokens only —
    mirrors account.py's ``pending_email IS NOT NULL`` email-change counterpart.
    """
    from paper_ingestion.routers.auth import RequestLinkBody, request_link

    fetchval_queries: list[str] = []

    async def _tracked_fetchval(query, *args):
        fetchval_queries.append(query)
        return None  # no recent login token → INSERT proceeds

    pool, conn = make_pool_and_conn(fetchrow_return={"id": 42})
    conn.fetchval = AsyncMock(side_effect=_tracked_fetchval)
    conn.execute = AsyncMock(return_value=None)

    request = _build_request(pool)
    body = RequestLinkBody(email="scope@example.com")

    with patch("paper_ingestion.routers.auth.send_magic_link", AsyncMock()):
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            await request_link(
                body=body,
                request=request,
                background_tasks=BackgroundTasks(),
            )

    cooldown_probes = [
        q for q in fetchval_queries if "magic_link_tokens" in q and "created_at" in q
    ]
    assert len(cooldown_probes) == 1, f"expected one cooldown probe, got: {cooldown_probes}"
    assert "pending_email IS NULL" in cooldown_probes[0], (
        f"cooldown probe must be login-scoped (pending_email IS NULL); got: {cooldown_probes[0]!r}"
    )


def test_build_magic_link_warns_on_unset_app_base_url_in_production(monkeypatch, caplog) -> None:
    """Production without APP_BASE_URL → the auth logger warns about the magic link.

    The warning must stay on this module's logger and name the magic link, so
    operators filtering by logger name still see it and can tell it apart from
    the invite-link warning the admin router emits.
    """
    import logging

    from paper_ingestion.routers.auth import _build_magic_link

    monkeypatch.delenv("APP_BASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")

    request = SimpleNamespace(
        url=SimpleNamespace(
            replace=lambda **kw: (
                "https://origin/auth/verify" + (f"?{kw['query']}" if kw.get("query") else "")
            )
        )
    )

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.routers.auth"):
        link = _build_magic_link(request, "tok123")

    assert link == "https://origin/auth/verify#token=tok123"
    assert "?token=" not in link
    warnings = [
        r
        for r in caplog.records
        if r.name == "paper_ingestion.routers.auth" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, (
        "Expected exactly one warning on the auth logger; got "
        f"{[(r.name, r.message) for r in caplog.records]}"
    )
    assert "APP_BASE_URL" in warnings[0].message
    assert "magic link" in warnings[0].message, (
        f"the warning must name the magic link; got: {warnings[0].message!r}"
    )


# ---------------------------------------------------------------------------
# enumeration resistance + swallowed-send event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_link_known_and_unknown_return_before_delayed_sender() -> None:
    """SMTP latency stays outside both known and unknown request paths."""
    from paper_ingestion.routers.auth import RequestLinkBody, request_link

    known_pool, known_conn = make_pool_and_conn(
        fetchrow_return={"id": 7},
        fetchval_return=None,
    )
    known_conn.execute = AsyncMock(return_value=None)
    unknown_pool, unknown_conn = make_pool_and_conn(fetchrow_return=None)
    unknown_conn.execute = AsyncMock(return_value=None)
    known_tasks = BackgroundTasks()
    unknown_tasks = BackgroundTasks()
    sender_release = asyncio.Event()

    async def _delayed_sender(*args, **kwargs):
        await sender_release.wait()
        return None

    with patch(
        "paper_ingestion.routers.auth.send_magic_link",
        AsyncMock(side_effect=_delayed_sender),
    ) as send_mock:
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            known = await asyncio.wait_for(
                request_link(
                    body=RequestLinkBody(email="known@example.com"),
                    request=_build_request(known_pool),
                    background_tasks=known_tasks,
                ),
                timeout=1.0,
            )
            unknown = await asyncio.wait_for(
                request_link(
                    body=RequestLinkBody(email="ghost@example.com"),
                    request=_build_request(unknown_pool),
                    background_tasks=unknown_tasks,
                ),
                timeout=1.0,
            )

    assert known.sent is True
    assert unknown.sent is True
    send_mock.assert_not_awaited()
    assert len(known_tasks.tasks) == 1
    assert unknown_tasks.tasks == []


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
                resp = await request_link(
                    body=body,
                    request=request,
                    background_tasks=BackgroundTasks(),
                )

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
    """The queued delivery task records a PII-safe SMTP failure event."""
    from paper_ingestion.routers.auth import RequestLinkBody, RequestLinkResponse, request_link

    pool, conn = make_pool_and_conn(fetchrow_return={"id": 7}, fetchval_return=None)
    conn.execute = AsyncMock(return_value=None)

    request = _build_request(pool)
    body = RequestLinkBody(email="relaydown@example.com")
    background_tasks = BackgroundTasks()

    with patch(
        "paper_ingestion.routers.auth.send_magic_link",
        AsyncMock(side_effect=RuntimeError("smtp down")),
    ):
        with patch("paper_ingestion.routers.auth.log_audit", AsyncMock()):
            with patch("paper_ingestion.routers.auth.log_event", AsyncMock()) as event_mock:
                resp = await request_link(
                    body=body,
                    request=request,
                    background_tasks=background_tasks,
                )
                event_mock.assert_not_awaited()
                await background_tasks()

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
                background_tasks = BackgroundTasks()
                resp = await request_link(
                    body=body,
                    request=request,
                    background_tasks=background_tasks,
                )

    assert resp.sent is True
    send_mock.assert_not_awaited()
    event_mock.assert_not_awaited()
    assert background_tasks.tasks == []
